"""采集测试证据：接口清单、响应耗时、摄像头/模型/检测的真实运行数据。

用法：D:\\py310\\python.exe docs\\testbook\\collect.py
输出：docs\\testbook\\evidence.json
"""
from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8160"
OUT = Path(__file__).resolve().parent / "evidence.json"


def get(path: str, timeout: float = 180.0):
    t0 = time.perf_counter()
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, time.perf_counter() - t0, body
    except urllib.error.HTTPError as exc:
        return exc.code, time.perf_counter() - t0, exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, time.perf_counter() - t0, str(exc).encode()


def req(path: str, payload=None, method: str | None = None, timeout: float = 180.0):
    t0 = time.perf_counter()
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    verb = method or ("POST" if payload is not None else "GET")
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=verb)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, time.perf_counter() - t0, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, time.perf_counter() - t0, exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, time.perf_counter() - t0, str(exc).encode()


def jget(path: str):
    code, ms, body = get(path)
    try:
        return code, ms, json.loads(body)
    except Exception:  # noqa: BLE001
        return code, ms, None


def main() -> None:
    ev: dict = {}

    # ---- 1) 接口清单 ----
    code, ms, spec = jget("/openapi.json")
    routes = []
    for path, ops in (spec or {}).get("paths", {}).items():
        for method, meta in ops.items():
            routes.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": (meta.get("summary") or "").strip(),
                    "tags": meta.get("tags") or [],
                }
            )
    routes.sort(key=lambda x: (x["path"], x["method"]))
    ev["routes"] = routes
    ev["route_count"] = len(routes)
    print(f"[1] 接口总数 {len(routes)}")

    def find(*keys, method=None):
        for r in routes:
            if all(k in r["path"] for k in keys) and (method is None or r["method"] == method):
                return r["path"]
        return None

    # ---- 2) 关键 GET 接口耗时（各 3 次取中位数）----
    lat = {}
    for p in [
        "/api/network",
        "/api/models",
        "/api/cameras",
        "/api/serial/ports",
        "/api/captures",
        "/api/annotate/images",
        "/api/annotate/reference",
        "/api/augment/ops",
        "/api/classes",
        "/api/state",
        "/api/detection/status",
        "/api/serial/status",
    ]:
        code, _, _ = get(p, timeout=60)
        if code is None:
            continue
        samples = []
        for _ in range(3):
            code, ms, _ = get(p, timeout=60)
            if code is not None:
                samples.append(round(ms * 1000, 1))
        if samples:
            lat[p] = {"status": code, "median_ms": statistics.median(samples), "samples_ms": samples}
    ev["latency"] = lat
    print(f"[2] 耗时采集完成 {len(lat)} 个接口")

    # ---- 3) 摄像头 ----
    code, ms, cams = jget("/api/cameras")
    ev["cameras"] = {"status": code, "ms": round(ms * 1000, 1), "data": cams}
    cam_index = None
    if isinstance(cams, dict) and cams.get("cameras"):
        cur = cams.get("current")
        cam_index = cur if cur is not None else cams["cameras"][0].get("index")
    print(f"[3] 摄像头 {len((cams or {}).get('cameras', []) if isinstance(cams, dict) else [])} 个, 选用 index={cam_index}")

    open_path = find("camera", "open") or find("camera", "select")
    if open_path and cam_index is not None:
        code, ms, body = req(open_path, {"index": cam_index})
        ev["camera_open"] = {"path": open_path, "status": code, "ms": round(ms * 1000, 1), "resp": body[:400].decode("utf-8", "ignore")}
        print(f"[3] 打开摄像头 {open_path} → {code} ({ms*1000:.0f}ms)")

    # ---- 4) 模型 ----
    code, ms, models = jget("/api/models")
    ev["models"] = {"status": code, "ms": round(ms * 1000, 1), "data": models}
    loaded = None
    if isinstance(models, dict):
        info = models.get("info") or {}
        loaded = info.get("path") or info.get("name")
    load_path = find("model", "load") or find("model", "select")
    if load_path:
        name = None
        if isinstance(models, dict):
            for m in models.get("models", []):
                if "trash_final0" in (m.get("name") or ""):
                    name = m.get("name")
                    break
            if name is None and models.get("models"):
                name = models["models"][0].get("name")
        if name:
            code, ms, body = req(load_path, {"name": name})
            ev["model_load"] = {"path": load_path, "name": name, "status": code, "ms": round(ms * 1000, 1), "resp": body[:400].decode("utf-8", "ignore")}
            print(f"[4] 加载模型 {name} → {code} ({ms*1000:.0f}ms)")
        else:
            ev["model_load"] = {"note": "未找到模型文件", "models": models}

    # ---- 5) 启动检测 ----
    start_path = find("detection", "start")
    if start_path:
        code, ms, body = req(start_path, {})
        ev["detection_start"] = {"path": start_path, "status": code, "ms": round(ms * 1000, 1), "resp": body[:400].decode("utf-8", "ignore")}
        print(f"[5] 启动检测 {start_path} → {code} ({ms*1000:.0f}ms)")

    # ---- 6) 检测状态与帧率 ----
    time.sleep(6)
    for p in ["/api/detection/status", "/api/state"]:
        code, ms, data = jget(p)
        ev.setdefault("runtime_status", {})[p] = {"status": code, "ms": round(ms * 1000, 1), "data": data}
    print("[6] 运行状态已采集")

    # ---- 7) 串口（无硬件时的降级行为）----
    code, ms, ports = jget("/api/serial/ports")
    ev["serial_ports"] = {"status": code, "ms": round(ms * 1000, 1), "data": ports}
    send_path = find("serial", "send") or find("serial", "sort")
    if send_path:
        code, ms, body = req(send_path, {"class_name": "paper", "slot": 1, "angle": 30, "force": True})
        ev["serial_send_without_hardware"] = {"path": send_path, "status": code, "ms": round(ms * 1000, 1), "resp": body[:600].decode("utf-8", "ignore")}
        print(f"[7] 无硬件发送分拣命令 → {code}")
    code, ms, sst = jget("/api/serial/status")
    ev["serial_status"] = {"status": code, "ms": round(ms * 1000, 1), "data": sst}

    # ---- 8) 分类映射 ----
    code, ms, classes = jget("/api/classes")
    ev["classes"] = {"status": code, "ms": round(ms * 1000, 1), "data": classes}

    OUT.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] 已写入 {OUT}  ({OUT.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
