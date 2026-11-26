# Brand assets (generated)

Scripts here build channel overlays. Their outputs are binaries and live in the
workspace (`WORKSPACE/assets/...`) and on Drive, not in git; re-run a script to
rebuild them.

## Like & subscribe overlay
`make_subscribe_anim.py`: a 4 s silent overlay. A thumbs-up bounces next to a
subscribe button, then a cursor clicks it and it turns into SUBSCRIBED.
It's an original design, with no channel's artwork and no YouTube logo.

    cd api
    uv run python ../tools/brand/make_subscribe_anim.py "C:/Users/taile/Videos/ai_video_editor_output/assets/subscribe_animation"

Writes:
- `subscribe_alpha.mov`: ProRes 4444 with alpha (Premiere, DaVinci, Vegas).
- `subscribe_alpha.webm`: VP9 with alpha.
- `subscribe_greenscreen.mp4`: for editors that only key out green.
- `preview.png`
- `frames/`

It needs Windows fonts (Segoe UI Emoji and Segoe UI Bold) and ffmpeg.
