import uvicorn


def dev():
    uvicorn.run("ai_video_editor.main:app", reload=True, port=8000)
