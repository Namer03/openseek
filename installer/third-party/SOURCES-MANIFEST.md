# OpenSeek installer - third-party native library source manifest

Scope: the Windows x86-64 wheel of PyAV `av` 18.1.0 (`cp311-abi3-win_amd64`) as installed in
the `av` and `av.libs` folders of the installed PyAV 18.1.0 Windows wheel.
Generated 2026-10-19. All archives are in `archives\` next to this file; licence texts are in `licenses\<component>\`.

## 1. Provenance of the bundle

- `av` 18.1.0 (PyPI) takes its FFmpeg build from the pyav-ffmpeg release **8.1.2-1**
  (`PyAV-Org/PyAV` tag `v18.1.0`, `scripts/ffmpeg-latest.json` ->
  `https://github.com/PyAV-Org/pyav-ffmpeg/releases/download/8.1.2-1/ffmpeg-{platform}.tar.gz`).
- pyav-ffmpeg tag `8.1.2-1` = commit `a71bf9279f7a4659154b68ba6783e89be460bcd5` (2026-06-24).
  Its `scripts/pkg.py` pins each source URL and SHA-256; every archive downloaded here was
  checked against those pinned SHA-256 values (all matched). `scripts/build-ffmpeg.py` holds the
  configure flags; `patches/*.patch` holds the patches (included in `pyav-ffmpeg-8.1.2-1.tar.gz`).
- The Windows wheel is built under MSYS2 MINGW64 (GCC 16.1.0 "Rev5"). Four DLLs come from the MSYS2
  toolchain (libiconv, zlib, libgcc/libstdc++, libwinpthread), not from pkg.py; the DLL file names
  carry a delvewheel hash suffix (the 32 hex digits are the leading digits of that DLL's SHA-256).
- Build-recipe patches that matter: `patches/ffmpeg.patch` (moves libx264/libx265 from the GPL list to the
  `--enable-version3` list in FFmpeg's `configure`), `patches/lame.patch` (build-system only),
  `patches/vpx.patch`, `patches/amf-headers.patch`, `patches/gmp.patch` (Linux only, unused).

## 2. Component table

"Identified by": DLL = read from the shipped DLL (strings or exported version API, via ctypes);
RECIPE = pinned in pyav-ffmpeg 8.1.2-1 `pkg.py`; MSYS2 = from the MSYS2 package set.

| Component | Version | Licence (upstream) | Role in bundle | Upstream URL | Archive (in `archives\`) | SHA-256 | Identified by |
|---|---|---|---|---|---|---|---|
| FFmpeg (libavcodec 62, libavformat 62, libavfilter 11, libavutil 60, libavdevice 62, libswscale 9, libswresample 6) | 8.1.2 | LGPL-2.1+ code, built with `--enable-version3` => LGPL-3.0+; the combined DLLs link GPL x264/x265, see section 4 | Decoding/encoding core used by PyAV | https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz | `ffmpeg-8.1.2.tar.xz` (+ `.asc`, `ffmpeg-devel.asc`) | `464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c` | DLL ("FFmpeg version 8.1.2", configure line) + RECIPE; GPG signature verified |
| x264 | core 165, git commit `b35605ace3ddf7c1a5d67a2eb553f034aef41d55` (snapshot, no release number) | GPL-2.0-or-later (also sold under a commercial licence) | H.264 encoder (libx264) | https://code.videolan.org/videolan/x264 | `x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.bz2` | `6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224` | DLL: core 165 (`x264_encoder_open_165`, PE version resource 0.165.0.0; also `X264_BUILD 165` in the archive's `x264.h`); commit from RECIPE only |
| x265 | 4.2+1-e444744 (tag 4.2, 1 commit after) | GPL-2.0-or-later (also commercial) | H.265/HEVC encoder (libx265, 8/10/12-bit) | https://bitbucket.org/multicoreware/x265_git | `x265_4.2.tar.gz` | `40b1ea0453e0309f0eba934e0ddf533f8f6295966679e8894e8f1c1c8d5e1210` | DLL (`x265_version_str` = `4.2+1-e444744`) matches `x265Version.txt` inside the archive |
| LAME | 3.100 | LGPL-2.0-or-later | MP3 encoder (libmp3lame) | https://lame.sourceforge.io/ (Debian orig tarball used by recipe) | `lame_3.100.orig.tar.gz` | `ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e` | DLL (`get_lame_version()` = 3.100); identical SHA-256 to the SourceForge `lame-3.100.tar.gz` |
| libiconv | 1.19 | LGPL-2.1+ (library; programs GPL, not shipped) | Character-set conversion, dependency of FFmpeg | https://ftp.gnu.org/gnu/libiconv/ | `libiconv-1.19.tar.gz` (+ `.sig`); MSYS2 packaging in `msys2-mingw-w64-libiconv-1.19-1.src.tar.zst` | `88dd96a8c0464eca144fc791ae60cd31cd8ee78321e67397e25fc095c4a19aa6` (msys2: `74428280c17094da5b702c29b2e1a0abae59556ea5dfdd65705cc8ccc1e000fb`) | DLL (`_libiconv_version` = 0x0113 => 1.19) and section-by-section match of the shipped DLL against the MSYS2 binary package `mingw-w64-x86_64-libiconv-1.19-1` (see section 7) |
| GCC runtime: libgcc_s_seh-1, libstdc++-6 | GCC 16.1.0 (MSYS2 "Rev5") | GPL-3.0-or-later WITH GCC Runtime Library Exception 3.1 | C++/unwinding runtime for x265/SVT-AV1 etc. | https://gcc.gnu.org / https://ftp.gnu.org/gnu/gcc/gcc-16.1.0/ | `msys2-mingw-w64-gcc-16.1.0-5.src.tar.zst` (contains `gcc-16.1.0.tar.xz`, its `.sig`, PKGBUILD and MSYS2 patches) | `947166ed372d28ab8ffa25a66bc5e37a60a42e96a686424ce58cad304fcaea15`; inner `gcc-16.1.0.tar.xz` = `50efb4d94c3397aff3b0d61a5abd748b4dd31d9d3f2ab7be05b171d36a510f79` | DLL (`GCC: (Rev5, Built by MSYS2 project) 16.1.0`); inner tarball hash equals PKGBUILD sha256 and its GPG signature verified; shipped libgcc_s/libstdc++ code and data sections match the MSYS2 binary package `gcc-libs-16.1.0-5` (section 7) |
| libwinpthread-1 (mingw-w64 winpthreads) | MSYS2 `mingw-w64-winpthreads` 14.0.0.r98.g19f5121a2-1 (mingw-w64 git commit `19f5121a2944e61ea5ea31af3667770d51b8ce8c`); the DLL is also identical to r92.g818fa6510 and the winpthreads source tree is identical between those two commits | MIT/ZPL-style (mingw-w64 winpthreads COPYING; some files BSD) | POSIX threads for MinGW code | https://www.mingw-w64.org/ | `msys2-mingw-w64-winpthreads-14.0.0.r98.g19f5121a2-1.src.tar.zst` (bare git clone of mingw-w64 at that commit + PKGBUILD + MSYS2 patch; 52 MB) | `b1444443b523b4e5264b40737d57f0e02eef957ca6ca05d771a033ecc8a81e4e` | Binary match (code/data sections) of the shipped DLL against MSYS2 binary packages libwinpthread r92 and r98, and not against r9, r47, r59, r147, r150, r179 (section 7) |
| zlib (zlib1.dll) | 1.3.2 | zlib licence | Compression | https://zlib.net/ | `zlib-1.3.2.tar.xz` (+ `.asc`); MSYS2 packaging `msys2-mingw-w64-zlib-1.3.2-2.src.tar.zst` | `d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3` (msys2 -2: `eef69dea52357e01b272d6fd6dc4d7c0773f71260cb7bcde6047c8c383518db3`) | DLL (`zlibVersion()` = 1.3.2); shipped `.rdata` equals MSYS2 package revision **-2**, not -1 (section 7); revision -1 archive removed |
| Opus | 1.6.1 | BSD-3-Clause | Opus decoder/encoder (libopus) | https://opus-codec.org/ | `opus-1.6.1.tar.gz` | `6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1` | DLL (`opus_get_version_string()` = libopus 1.6.1) + RECIPE |
| dav1d | 1.5.3 | BSD-2-Clause | AV1 decoder | https://code.videolan.org/videolan/dav1d | `dav1d-1.5.3.tar.bz2` | `e099f53253f6c247580c554d53a13f1040638f2066edc3c740e4c2f15174ce22` | DLL (`dav1d_version()` = 1.5.3) + RECIPE |
| SVT-AV1 | v4.1.0 | BSD-3-Clause-Clear + AOMedia patent licence 1.0 | AV1 encoder (libSvtAv1Enc) | https://gitlab.com/AOMediaCodec/SVT-AV1 | `SVT-AV1-v4.1.0.tar.bz2` | `184162d3db3a4448882b17230413b4938ca252eef6b3c5e2f1236b2fcf497881` | DLL (`svt_av1_get_version()` = v4.1.0) + RECIPE |
| libvpx | v1.16.0 | BSD-3-Clause + WebM patent grant | VP8/VP9 codec | https://github.com/webmproject/libvpx | `vpx-1.16.0.tar.gz` | `7a479a3c66b9f5d5542a4c6a1b7d3768a983b1e5c14c60a9396edc9b649e015c` | DLL (`vpx_codec_version_str()` = v1.16.0) + RECIPE (patched by `vpx.patch`) |
| libwebp, libwebpmux, libsharpyuv | 1.6.0 | BSD-3-Clause + WebM patent grant | WebP encoder/decoder, muxer, YUV helper | https://github.com/webmproject/libwebp | `webp-1.6.0.tar.gz` | `93a852c2b3efafee3723efd4636de855b46f9fe1efddd607e1f42f60fc8f2136` | DLL (`WebPGetEncoderVersion()`/`WebPGetMuxVersion()` = 0x010600) + RECIPE; libsharpyuv is built from the same tree (version not separately readable) |
| Intel oneVPL dispatcher (libvpl) | 2.16.0 | MIT | Intel Quick Sync dispatcher (loads GPU runtime if present) | https://github.com/intel/libvpl | `libvpl-2.16.0.tar.gz` | `d60931937426130ddad9f1975c010543f0da99e67edb1c6070656b7947f633b6` | DLL: PE version resource FileVersion 2.16.0.0 (confirmed from binary) + RECIPE |
| OpenCORE AMR (libopencore-amrnb, libopencore-amrwb) | 0.1.6 | Apache-2.0 | AMR-NB/WB decoding | https://sourceforge.net/projects/opencore-amr/ | `opencore-amr-0.1.6.tar.gz` | `483eb4061088e2b34b358e47540b5d495a96cd468e361050fae615b1809dc4a1` | RECIPE only: DLLs have no version string, resource or version API; only the libtool soname (`-0`) is visible |
| nv-codec-headers (headers compiled into avcodec; NVENC/NVDEC loaded dynamically from the NVIDIA driver) | n13.0.19.0 | MIT | Build-time headers only; no DLL | https://github.com/FFmpeg/nv-codec-headers | `nv-codec-headers-n13.0.19.0.tar.gz` | `86d15d1a7c0ac73a0eafdfc57bebfeba7da8264595bf531cf4d8db1c22940116` | RECIPE (configure line has `--enable-nvenc --enable-nvdec`) |
| AMD AMF headers (AMF loaded dynamically from the AMD driver) | v1.5.0 | MIT (with AMD standards/patent notice) | Build-time headers only; no DLL | https://github.com/GPUOpen-LibrariesAndSDKs/AMF | `AMF-headers-v1.5.0.tar.gz` | `d569647fa26f289affe81a206259fa92f819d06db1e80cc334559953e82a3f01` | RECIPE (configure line has `--enable-amf`) |
| pyav-ffmpeg build recipe (scripts + patches) | tag 8.1.2-1 (`a71bf9279f7a4659154b68ba6783e89be460bcd5`) | No LICENSE file in the repo; recipe only, not part of the binaries | Exact versions, flags, patches | https://github.com/PyAV-Org/pyav-ffmpeg | `pyav-ffmpeg-8.1.2-1.tar.gz` | `a3bd63dba5336ec47766f8e59ba065d16342002d922c2f96cfed9d1a9b6da6d8` (GitHub tag tarball; its hash is not guaranteed stable) | git tag |
| PyAV `av` (Python binding, Cython code) | 18.1.0 | BSD-3-Clause | The `av\` package | https://github.com/PyAV-Org/PyAV | `av-18.1.0.tar.gz` | `47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28` (equals the PyPI-published digest) | `METADATA` of installed wheel; PyPI JSON |

Not bundled as separate DLLs (confirmed): libpng (built by the recipe but not enabled in FFmpeg and not present in
`av.libs`), nasm (build tool), GnuTLS/gmp/nettle/alsa (Linux only).

## 3. FFmpeg configuration found in the DLLs (identical in avcodec, avutil, avfilter, swscale, swresample, avdevice)

```
--prefix=/c/cibw/vendor --disable-programs --disable-doc --disable-libxml2 --disable-lzma
--disable-libtheora --disable-libfreetype --disable-libfontconfig --disable-libbluray
--disable-libopenjpeg --enable-mediafoundation --enable-version3 --disable-alsa --disable-gnutls
--enable-libdav1d --enable-libmp3lame --enable-libopencore-amrnb --enable-libopencore-amrwb
--enable-libopus --enable-libsvtav1 --enable-libvpx --enable-libwebp --disable-libxcb --enable-zlib
--enable-libx264 --enable-libx265 --enable-nvenc --enable-nvdec --enable-amf --enable-libvpl
--disable-encoder='avui,dca,mlp,opus,s302m,sonic,sonic_ls,truehd' --disable-decoder=sonic
--disable-libjack --disable-indev=jack --disable-filter=gfxcapture
```

Notes: no `--enable-gpl` and no `--enable-nonfree`. `avcodec_license()` in the DLLs reports "LGPL version 3 or later"
(this is a consequence of the patch that lists x264/x265 under version3; see section 4). FFmpeg version string: "FFmpeg version 8.1.2".

## 4. Findings that affect compliance

1. **The DLLs' self-reported licence label is misleading.** Upstream FFmpeg documents libx264/libx265 as GPL
   (needs `--enable-gpl`). pyav-ffmpeg patches `configure` so they build with only `--enable-version3`, therefore
   the libraries report "LGPL version 3 or later". The shipped libavcodec etc. nevertheless link against libx264 and libx265
   (GPL-2.0-or-later), so the combined FFmpeg libraries must be treated and distributed as **GPL-3.0-or-later**
   (v3 because of Apache-2.0 OpenCORE and `--enable-version3`; x264/x265/LAME are "or later", so v3 is reachable).
   The installer notices must say GPL, not LGPL.
2. **AGPL-3.0-or-later and GPL-3.0-or-later**: compatible in both directions (GPLv3 section 13 / AGPLv3 section 13). OpenSeek
   calls the libraries through PyAV; combined distribution as AGPL-3.0-or-later for OpenSeek's own code with GPL-3.0-or-later
   libraries is fine. GPL-2.0-only code would not be, but none was found (x264, x265, LAME are all "or later" in their headers).
3. **Apache-2.0 (OpenCORE-AMR)** is incompatible with GPLv2 but compatible with GPLv3 - fine here because everything is combined at v3.
4. **IJG credit**: FFmpeg's `LICENSE.md` says that if you only distribute executables you must credit the Independent JPEG
   Group in the accompanying documentation (files `jfdctfst.c`, `jfdctint_template.c`, `jrevdct.c`) and indicate changes. The
   FFMPEG-NOTICE.txt includes the sentence "This software is based in part on the work of the Independent JPEG Group."
5. **Attribution-required licences (BSD/MIT/Apache/zlib)**: opus, dav1d, SVT-AV1, libvpx, libwebp, PyAV (BSD), libvpl, nv-codec-headers,
   AMF headers, winpthreads (MIT-like), OpenCORE (Apache-2.0: ship LICENSE; no NOTICE file exists in 0.1.6), zlib (not required but courteous).
   Binary redistributions of BSD components must reproduce the copyright notice and conditions: ship `licenses\` with the installer.
   SVT-AV1 and libvpx/libwebp additionally carry patent grants (PATENTS files included).
6. **GPL source offer scope**: x264, x265, FFmpeg (GPL-combined), LAME, libiconv, GCC runtime all require corresponding source for the DLLs we ship;
   covered by the archives. For the GCC runtime, the source package of the exact MSYS2 build (patches, PKGBUILD) is included.
7. **Not a copyright-licence matter but worth knowing**: H.264, HEVC, AAC, MP3, AMR codecs are covered by patent pools; x264/x265
   and AMD AMF headers state they grant no patent licence. This is outside this task; flag for the maintainer.
8. **No non-free components found**: no fdk-aac, no OpenSSL, no `--enable-nonfree`. NVENC/AMF/QSV are loaded at runtime from vendor
   drivers (not redistributed).

## 5. What could not be verified (honest list)

- OpenCORE-AMR 0.1.6: recipe only (nothing in the DLLs identifies the version). Acceptable because the recipe is what produced the
  binaries and there is no other 0.1.x release the recipe could have meant; but a binary-level confirmation is impossible.
- x264 commit `b35605ace3dd...`: recipe only. The DLL proves only API/core 165 (the archive's `x264.h` also says 165); a tarball build carries no git revision.
- libwinpthread: the shipped DLL matches both MSYS2 r92 and r98 byte-for-byte in code/data sections, and the winpthreads source tree is
  identical between those two mingw-w64 commits, so the r98 source package is exactly corresponding either way. Which one was installed on the CI runner cannot be told apart.
- Section 7 compares sections (.text/.rdata/.data) of the shipped DLLs with MSYS2 binary packages; the full-file hash cannot be compared because the recipe strips the DLLs and delvewheel renames/patches them.
- Integrity: FFmpeg (GPG, key FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8 matches published fingerprint), libiconv (GPG, Bruno Haible, GNU keyring),
  zlib (GPG, Mark Adler, key fetched from keys.openpgp.org), GCC (GPG, Jakub Jelinek, GNU keyring) verified. Keys are "unknown trust" in a fresh keyring - no web-of-trust check was done.
  Every pyav-ffmpeg-pinned archive (FFmpeg, x264, x265, LAME, opus, dav1d, SVT-AV1, libvpx, libwebp, opencore-amr, libvpl, nv-codec-headers, AMF headers) matched the recipe's
  pinned SHA-256. av-18.1.0.tar.gz matches the PyPI digest. LAME matches the SourceForge download byte-for-byte. Not available upstream: signatures/hashes for x264, x265, dav1d, opus, SVT-AV1, libvpx, libwebp, libvpl, opencore-amr
  (verified only against the recipe's pins); the MSYS2 source packages themselves have no signature check (they were fetched over HTTPS from repo.msys2.org; the inner upstream tarballs were checked as stated).
- The pinned hashes in pyav-ffmpeg are from the same repo that builds the binaries; I have not rebuilt the DLLs to confirm byte-for-byte reproducibility.
- Obligation interpretation is mine, not legal advice.

## 6. Shipped DLL SHA-256 (for tying this manifest to the exact binaries)

```
777704d8914bb99e5d78f3a0e7424031b5376621f2e7c60e83b84e77fcc7e2d7 avcodec-62-984de33114b7fa384296817dec999c9d.dll
f97c1c7539fddbf5d713c0904bb4340f9000705bdb2d950338f6b39dba20cb3a avdevice-62-bf7a3ffbb6a4b577f24a39ea98a958d5.dll
26e5bd7b01ad8874db7842c03f4ee0fa1466f1192f2f245c1cdccc8acb81ebd2 avfilter-11-aef80fc767dc77e0319469b5bdcdf998.dll
a3494c5304c387518e0d5136ed9bf5e5fc1a3b227ce3f380357149ee09717ff2 avformat-62-b6d6bb16ff0b7753371e2d0b285c9dc0.dll
ff4aae43357698825fa5392764d3b680e36c1846b75677bdbf313d8c141ece6d avutil-60-cc1777f859dcfd98b8019bdf459e774e.dll
c4dd99c98ddcdb013e820dbd68a30e26472894f1d788e2862376e0404542f045 libSvtAv1Enc-c4dd99c98ddcdb013e820dbd68a30e26.dll
959ebc1340f7dfcbcf8e299a26281738d73b53c2da0a3ceb1a1df80ac0c41b4b libdav1d-959ebc1340f7dfcbcf8e299a26281738.dll
99a25df00a49a5e1ff131533416faa8d4686dd09a9a800633bc56ce93f69b5a6 libgcc_s_seh-1-4c0a762e4178b574f72d1ef1f8bb5fc9.dll
6ce5f4ff92ada49d6f23a8e4134555028a60cbf22ac4bb5ac78b12a2727676ec libiconv-2-6ce5f4ff92ada49d6f23a8e413455502.dll
da02696cc34a9bbc1d1d4f67a85cfbef40a699da443aadb03551d960d2909ea7 libmp3lame-0-da02696cc34a9bbc1d1d4f67a85cfbef.dll
b8933128305cd084d40426d0d2c0d9ef8c9a52b10a383aac7ec24fed03f499ad libopencore-amrnb-0-b8933128305cd084d40426d0d2c0d9ef.dll
49da3acfc50704f92538d74aca03c3b838195ae4de88be98667e869c2aba0477 libopencore-amrwb-0-49da3acfc50704f92538d74aca03c3b8.dll
4369edc456631a3cc933d7918747e5d2d111056dbc8a85b01330b5a53c062d44 libopus-0-4369edc456631a3cc933d7918747e5d2.dll
8b869e30854bf3db317a2a93d0782c3df2e9a03d1360320815257f0cbd306c63 libsharpyuv-8b869e30854bf3db317a2a93d0782c3d.dll
80c66b5220c554aa61b72cce0e9322547d111f81179792dde4d9a645255d0e5a libstdc++-6-2d5c346d47ad531ef9f5185db0e8cef3.dll
96a2ee0b9d4645791485f0f897fd4ea4d77f324e11918dbc10466fc949f7dca4 libvpl-969a52d626b0390fc34f6a4fb0b08d7e.dll
e0be857ba4f86709addb42c5e81fef2f565faf52c13fdb982f393ff74d92a0ca libvpx-1-d869f8b2cb42d58b35545ffbfb0f7509.dll
5f044e8cf8d0347fdd4d5fff197a64c20b9e2356ee1dd84c11a19638fd9901eb libwebp-4bf4e57964c7a01c09ce39470d6b67ca.dll
8394a4928195acdde4d7a5c3cc1f11b581890d061ca4b25ac35c8745376d06ae libwebpmux-9cf6581f8709616d9591befbb1a2264b.dll
02332fab016bf884a2a7a7b2ece22ffe1dea58d56609f913026c22697dd950bc libwinpthread-1-02332fab016bf884a2a7a7b2ece22ffe.dll
f3a909470ddc2d85ed21eda3d0fb795404382727c59efa502e3b71cb5ef1aebc libx264-165-f3a909470ddc2d85ed21eda3d0fb7954.dll
57b1a6cad779aa841985cbdc22539802dd0c15f5832f1783bf38e72e09929f60 libx265-efe48a158520a59ef99c0a0b3eb835ae.dll
44b6c43045b783970832cd5cac9b3a7250a79476ca34cf90c98f02428749c8b0 swresample-6-0158536d5c4197d7d623445553ce9472.dll
134b9ba046e46a4391a63d391635a0fc2ba12769295ffc0ffd9b2e78f1376472 swscale-9-0c9886c118598c54e159ef2267ff99f3.dll
79e0c4f9db71cb511398c504b832d395ba0fdee530f98efd1f861dd96de2ceb2 zlib1-79e0c4f9db71cb511398c504b832d395.dll
```

## 7. Binary match of the MSYS2-sourced DLLs

Method: downloaded the MSYS2 MINGW64 binary packages from https://repo.msys2.org/mingw/mingw64/ and compared SHA-256 of the PE sections
`.text`, `.rdata`, `.data` (headers/symbols excluded, because the recipe runs `strip -s` and delvewheel renames the files) with the DLLs in `av.libs`.

| Shipped DLL | MSYS2 binary package compared | Result |
|---|---|---|
| libgcc_s_seh-1, libstdc++-6 | gcc-libs 16.1.0-5 (built 2026-05-17) | `.text` and `.rdata` match; `.data` differs in both (initialised data, likely altered by strip/delvewheel; not investigated) -> GCC 16.1.0-5 |
| libiconv-2 | libiconv 1.19-1 (built 2026-03-14) | all three sections match |
| zlib1 | zlib 1.3.2-1 and 1.3.2-2 | `.text` and `.data` match both; `.rdata` matches **-2 only** -> revision -2 |
| libwinpthread-1 | libwinpthread r9, r47, r59, r92, r98, r147, r150, r179 | matches r92.g818fa6510 and r98.g19f5121a2 (identical to each other); no match for the others. The CI build date (tag 2026-06-24, release assets 2026-06-29) is later than r147 (2026-06-26), so the runner evidently had r92/r98 installed (cached image) |

## 8. Verification status (one line per component)

- FFmpeg 8.1.2: verified by GPG signature (published ffmpeg-devel key), by recipe pin SHA-256, and by binary string ("FFmpeg version 8.1.2", configure line).
- x264 core 165: verified by binary (export name, PE version 0.165.0.0). Commit b35605ace3dd: recipe pin SHA-256 only (archive hash matches the pin; commit not provable from binary).
- x265 4.2+1-e444744: verified by binary string and by `x265Version.txt` in the archive; recipe pin SHA-256 matches. No upstream signature.
- LAME 3.100: verified by binary (`get_lame_version`), recipe pin SHA-256, and identical to the SourceForge download.
- libiconv 1.19: verified by GPG signature, by binary (`_libiconv_version`), and by section match with MSYS2 package 1.19-1.
- GCC runtime 16.1.0: verified by GPG signature of the inner tarball, binary string (Rev5) and section match with MSYS2 gcc-libs 16.1.0-5.
- libwinpthread: verified by section match with MSYS2 r92/r98 (source r98 kept); the MSYS2 source package itself is unsigned (HTTPS only).
- zlib 1.3.2: verified by GPG signature, binary (`zlibVersion`) and MSYS2 revision -2 by section match.
- Opus 1.6.1, dav1d 1.5.3, SVT-AV1 4.1.0, libvpx 1.16.0, libwebp 1.6.0 (+mux, sharpyuv): verified by binary version API/string (dav1d also PE resource) and by recipe pin SHA-256; no upstream signature.
- libvpl 2.16.0: verified by binary (PE version resource 2.16.0.0) and by recipe pin SHA-256.
- OpenCORE-AMR 0.1.6: recipe pin SHA-256 only; version unverifiable from the binary.
- nv-codec-headers n13.0.19.0, AMF headers 1.5.0: recipe pin SHA-256 only (header-only, compiled into FFmpeg; no DLL).
- PyAV 18.1.0: verified by PyPI digest and installed wheel METADATA.
- pyav-ffmpeg 8.1.2-1: git tag referenced by PyAV 18.1.0's `ffmpeg-latest.json`; GitHub tag tarball hash not stable.
