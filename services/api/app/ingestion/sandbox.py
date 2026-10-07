"""Run untrusted-input parsing in a separate process with a hard timeout (spec §13).

Process isolation contains parser crashes, hangs and memory blow-ups. On POSIX the child also gets
address-space and CPU limits. Network and filesystem isolation (no outbound network, read-only FS,
non-root) are deployment controls applied to the worker container; see docs/runbooks/ingestion.md.
"""

import multiprocessing as mp
import sys
from typing import Any

from app.ingestion import parsers, sniff

PARSE_TIMEOUT_SECONDS = 60
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

_FUNCTIONS = {
    "txt": parsers.parse_txt,
    "xlsx": parsers.parse_xlsx,
    "docx": parsers.parse_docx,
    "pdf": parsers.parse_pdf,
    "render_pdf_page": parsers.render_pdf_page,
    "normalize_image": parsers.normalize_image,
    "inspect": lambda data, ext: sniff.inspect(data, ext).__dict__,
}


def _limit_resources() -> None:
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (PARSE_TIMEOUT_SECONDS, PARSE_TIMEOUT_SECONDS))


def _child(conn, name: str, args: tuple) -> None:
    try:
        _limit_resources()
        conn.send(("ok", _FUNCTIONS[name](*args)))
    except parsers.ParseError as exc:
        conn.send(("parse_error", (exc.code, exc.message)))
    except sniff.Rejected as exc:
        conn.send(("rejected", (exc.code, exc.message)))
    except MemoryError:
        conn.send(("parse_error", ("RESOURCE_LIMIT", "The file needs too much memory to read.")))
    except Exception as exc:  # noqa: BLE001 - any parser crash is reported, not raised across processes
        conn.send(("error", type(exc).__name__))
    finally:
        conn.close()


def run(name: str, *args: Any, timeout: float = PARSE_TIMEOUT_SECONDS) -> Any:
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(child, name, args), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise parsers.ParseError("PARSE_TIMEOUT", "Reading the file took too long.")
        try:
            status, payload = parent.recv()
        except EOFError as exc:  # child died (e.g. killed by a resource limit)
            raise parsers.ParseError("PARSE_CRASHED", "The file could not be read.") from exc
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        parent.close()
    if status == "ok":
        return payload
    if status == "parse_error":
        raise parsers.ParseError(*payload)
    if status == "rejected":
        raise sniff.Rejected(*payload)
    raise parsers.ParseError("PARSE_FAILED", "The file could not be read. Check that it opens normally.")
