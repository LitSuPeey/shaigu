# -*- coding: utf-8 -*-
"""合集·筛股 2.0 — A 股量化选股合并工具（FastAPI 服务）。"""
import logging
import os
import sys

# 加入项目根到 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ★ 进程级网络修复（2026-10-14）：必须在任何网络请求发生之前安装。
#   解决两类实测问题（详见 app/core/netfix.py）：
#   1) 行情域名 IPv6 优先 →本机 IPv6 通道异常 → 东财请求全量失败 → 拉数据巨慢；
#   2) 从注入了 HTTP(S)_PROXY 终端启动时 requests 全量 ProxyError。
from app.core import netfix as _netfix
_netfix.install()

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core import db as core_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

DB_PATH = os.environ.get(
    "UNIFIED_DB_PATH",
    os.path.join(_ROOT, "data", "unified_data.db"))
core_db.set_db_path(DB_PATH)

app = FastAPI(title="合集·筛股 2.0", version="2.0")
app.include_router(router)

WEB_DIR = os.path.join(_HERE, "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/memes/{name}")
def meme(name: str):
    path = os.path.join(WEB_DIR, "memes", name)
    if not os.path.exists(path) or ".." in name:
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/")
def root():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon():
    """浏览器自动请求的站点图标——无图标文件，返回 204 避免服务窗口 404 噪音。"""
    from fastapi import Response
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    """Chrome/Edge 打开本地页面时自动发出的 DevTools 探测（与程序无关）——
    返回空配置，消除服务窗口的 404 噪音。"""
    return {}


@app.on_event("startup")
def _startup():
    log.info(f"DB path: {DB_PATH}")
    # 预热 akshare（含 py_mini_racer/V8 DLL）：必须在主线程完成首次加载——
    # 事件日志证实后台线程首次加载 mini_racer.dll 会以 0x80000003 无声崩溃
    try:
        from app.core import sync as _s
        _s.warmup_ak()
        log.info("akshare 预热完成")
    except Exception as e:
        log.warning(f"akshare 预热失败（不影响启动，同步时再加载）: {e}")
    try:
        conn = core_db.writer()          # 自动创建 data 目录与库文件
        core_db.ensure_schema(conn)      # 空库也建好全部表，后续刷新信息/同步即可灌数据
        s = core_db.db_stats(conn)
        log.info(f"DB ready: stocks={s['stocks']} daily_rows={s['daily_rows']} "
                 f"val_rows={s['val_rows']} daily_max={s['daily_max']}")
        if s["stocks"] == 0:
            log.warning("空库：请先在网页右上角点「刷新信息」拉取股票列表，"
                        "再点「同步数据」拉取日线/估值/分红（需联网）")
    except Exception as e:
        log.error(f"DB 初始化失败: {e}")
    # 快照表为空时后台自动构建（估值/股息条件依赖）
    try:
        from app.core import snapshot as _snap
        if not _snap.snapshot_ready():
            log.info("快照表为空，后台构建中…")
            import threading
            threading.Thread(
                target=lambda: _snap.build_snapshot() and None,
                daemon=True).start()
    except Exception as e:
        log.warning(f"快照检查失败: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")