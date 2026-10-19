# Test fixtures (synthetic media)

Some tests need real video files (range reads, tile extraction, alternative-source matching). They are generated
locally from synthetic content (procedural gradients + grain + a frame-number barcode); no copyrighted material is
used and nothing is committed (`generated/` is git-ignored).

Requirements: `ffmpeg` on PATH (with libx264) and about 10 GB of disk and a few minutes of CPU for the default set (the 120 s and 600 s masters are near-lossless; delete `master_*` afterwards, tests do not read them).

    python backend/tests/fixtures/gen_altsrc.py                 # default: 10 min variants + 120 s / 600 s masters
    python backend/tests/fixtures/gen_altsrc.py --no-master     # only the 10 min set (skips test_alt_e2e)
    python backend/tests/fixtures/gen_altsrc.py 2h              # also the 2 h pair (large, slow; not needed by the suite)
    python backend/tests/fixtures/gen_master.py 600 --out DIR   # one master set by hand

Output goes to `$OPENSEEK_TEST_FIXTURES`, default `backend/tests/fixtures/generated`. Point the variable at another
directory to keep fixtures elsewhere (generators take `--out DIR` too). Tests that need the files skip with a message
when they are missing; the rest of the suite does not need them.

Other optional environment variables:

- `OPENSEEK_NUVIO_CLIENT_DIR`: checkout of the separate Nuvio client (must contain `tests/openseek_guest_cli.c`);
  enables the client-protocol tests, which also need WSL or a native `cc`. Skipped otherwise.
