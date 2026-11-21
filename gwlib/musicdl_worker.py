# -*- coding: utf-8 -*-
"""musicdl 单源 worker 子进程：一进程一音源，一次一任务。

JSON 行协议（UTF-8）：
  stdin   <- {"op": "search", "source": ..., "keyword": ..., "fetch_size": ...,
              "limit": ..., "song_id": str|null, "budget_s": float}
  stdout  -> {"type": "ready"}
          -> {"type": "entry", "entry": [item, download_headers, lyric]}
          -> {"type": "result", "count": n, "partial": bool}
          -> {"type": "error", "error": "TypeName (stage=search)"}

标准输出先改道 stderr，协议走保留的原始 stdout，避免第三方库 print 污染。
"""
import json
import os
import sys
import threading

from . import __version__ as VERSION


def _emit(proto_fd: int, message: dict) -> None:
    data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        view = view[os.write(proto_fd, view):]


def main() -> int:
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    import logging
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s [%(levelname)s] [v" + VERSION + "] %(name)s: %(message)s")
    logging.getLogger("musicdl").setLevel(logging.ERROR)

    try:
        from . import musicdl_core as search_core
    except ImportError:
        import musicdl_core as search_core

    _emit(proto_fd, {"type": "ready"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            task = json.loads(raw)
            if not isinstance(task, dict):
                raise ValueError("task must be a JSON object")
        except ValueError:
            _emit(proto_fd, {"type": "error", "error": "invalid task"})
            continue
        try:
            stages = []
            if task.get("op") == "official_search":
                result = search_core.run_official_search_task(
                    task, lambda entry: _emit(proto_fd, {"type": "entry", "entry": entry}))
            else:
                result = search_core.run_search_task(
                    task, lambda entry: _emit(proto_fd, {"type": "entry", "entry": entry}),
                    on_stage=stages.append)
            _emit(proto_fd, {"type": "result",
                             "count": int(result.get("count") or 0),
                             "partial": bool(result.get("partial"))})
        except Exception as exc:
            stage = getattr(exc, "stage", None) or (stages[-1] if stages else "unspecified")
            _emit(proto_fd, {"type": "error",
                             "error": search_core.describe_task_error(exc, stage)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
