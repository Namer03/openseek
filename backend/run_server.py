"""PyInstaller entry point for the OpenSeek server.

`OpenSeekServer.exe serve|pair|devices|dashboard|...` behaves like `python -m openseek`.
`OpenSeekServer.exe --openseek-worker` is the extraction worker child (stdin/stdout JSON lines),
spawned by openseek.worker.WorkerProcess when running frozen.
"""
import multiprocessing
import sys


def _run() -> int:
    if sys.argv[1:2] == ["--openseek-worker"]:
        from openseek.worker import main as worker_main
        worker_main()
        return 0
    from openseek.cli import main
    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(_run())
