from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .routers import (
    analyze,
    assets,
    clips,
    edits,
    feedback,
    intros,
    jobs,
    league,
    projects,
    sfx,
    shorts,
    vlm,
)
from .tracing import flush_tracing, init_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    init_tracing()
    yield
    flush_tracing()


app = FastAPI(title="AI Video Editor", version="0.1.0", lifespan=lifespan)

app.include_router(assets.router, prefix="/api/v1")
app.include_router(clips.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(analyze.router, prefix="/api/v1")
app.include_router(edits.router, prefix="/api/v1")
app.include_router(sfx.router, prefix="/api/v1")
app.include_router(league.router, prefix="/api/v1")
app.include_router(intros.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(vlm.router, prefix="/api/v1")
app.include_router(shorts.router, prefix="/api/v1")

# Serve workspace files directly so the webapp's <video> elements can
# stream `compilation.mp4`, fetch `thumbnail.jpg`, etc. Source recordings
# (OUTPLAYED_MEDIA_DIR) are NOT exposed — only outputs the system itself
# produces. StaticFiles supports HTTP Range, which <video> needs to seek.
# `check_dir=False` lets the mount survive tests that point `workspace_dir`
# at a tmp path that doesn't exist until the lifespan hook creates it.
app.mount(
    "/workspace",
    StaticFiles(directory=settings.workspace_dir, check_dir=False),
    name="workspace",
)


@app.get("/health")
async def health():
    return {"status": "ok"}
