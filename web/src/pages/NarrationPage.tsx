import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { narratedVideoUrl } from "@/api/narration";
import { CameraBoxEditor } from "@/components/narration/CameraBoxEditor";
import { CameraSettings } from "@/components/narration/CameraSettings";
import { ExportPanel } from "@/components/narration/ExportPanel";
import { HookChangesBadge } from "@/components/narration/HookChangesBadge";
import { HookInspector } from "@/components/narration/HookInspector";
import { MixPanel } from "@/components/narration/MixPanel";
import { RecordControls } from "@/components/narration/RecordControls";
import { StoryboardPanel } from "@/components/narration/StoryboardPanel";
import { SuggestionsPanel } from "@/components/narration/SuggestionsPanel";
import { TakeInspector } from "@/components/narration/TakeInspector";
import { Timeline } from "@/components/narration/timeline/Timeline";
import { VideoPlayer } from "@/components/VideoPlayer";
import { VideoSwitcher } from "@/components/VideoSwitcher";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";
import { useAudioContext } from "@/hooks/useAudioContext";
import { useAudioLevel } from "@/hooks/useAudioLevel";
import { useFrameStrip } from "@/hooks/useFrameStrip";
import { useHookPicks } from "@/hooks/useHookPicks";
import { useJob } from "@/hooks/useJob";
import { useMediaDevices } from "@/hooks/useMediaDevices";
import { useMediaStream } from "@/hooks/useMediaStream";
import { useMixPreview } from "@/hooks/useMixPreview";
import { useNarrationSession } from "@/hooks/useNarrationSession";
import { usePeaks } from "@/hooks/usePeaks";
import { useStoryboard } from "@/hooks/useStoryboard";
import { useTakeBuffers } from "@/hooks/useTakeBuffers";
import { useTakeCapture } from "@/hooks/useTakeCapture";
import { useVideoDuration } from "@/hooks/useVideoDuration";
import { layout, toClips, type TrackClip } from "@/lib/hookTrack";
import { clampRegions } from "@/lib/regions";
import { effectiveStart } from "@/lib/takeEdits";
import type { TargetRef } from "@/types/narration";

/**
 * `/narrate/:kind/:key` — record voice (and optionally camera) takes over
 * a finished video, edit them on the timeline (trim, nudge, split, mute,
 * gain), shape the game audio (regions, mix), place the camera box, export
 * `narrated.mp4`.
 *
 * Nothing here knows about a game or a kind: the target comes from the
 * route, the video URL and camera layout from the API.
 */
export function NarrationPage() {
  const { kind, key } = useParams<{ kind: string; key: string }>();
  if (!kind || !key) return null;
  // Keyed so switching targets resets every piece of local state.
  return <NarrationWorkspace key={`${kind}/${key}`} target={{ kind, key }} />;
}

function NarrationWorkspace({ target }: { target: TargetRef }) {
  const narration = useNarrationSession(target);
  const view = narration.session.data;

  // Media input
  const [cameraOn, setCameraOn] = useState(false);
  const [microphoneId, setMicrophoneId] = useState("");
  const [cameraId, setCameraId] = useState("");
  // The mic/camera are held only while checking the mic or recording a take,
  // never just because the page is open.
  const [checkingMic, setCheckingMic] = useState(false);
  const [armed, setArmed] = useState(false); // Record pressed, until the take ends
  const media = useMediaStream({
    enabled: checkingMic || armed,
    camera: cameraOn,
    microphoneId,
    cameraId,
  });
  const devices = useMediaDevices(media.stream);
  const level = useAudioLevel(media.stream);

  // Video + takes
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  // Only the length: the playhead is read inside the timeline and the take
  // panel, so this page does not re-render while the video plays.
  const durationS = useVideoDuration(video);
  const capture = useTakeCapture({
    video,
    stream: media.stream,
    onTake: (take) => narration.addTake.mutate(take),
  });

  // Timeline data: frames + game peaks from the API, take waveforms decoded here.
  const frames = useFrameStrip(target);
  const peaks = usePeaks(target);
  const audio = useAudioContext();
  const { buffers, waveforms } = useTakeBuffers(view, audio.ctx);
  // The full mix (game audio + every take, ducked) plays through Web Audio.
  useMixPreview({ video, session: view?.session, buffers, enabled: !capture.recording, audio });
  const [selectedTakeId, setSelectedTakeId] = useState<string | null>(null);

  // The hook track (joined videos whose map has a hook block); one selection
  // on the timeline: a take or a hook clip.
  const hookPicks = useHookPicks(target);
  const hookView = hookPicks.view;
  const storyboard = useStoryboard(target, !!hookView);
  const [selectedHookId, setSelectedHookId] = useState<string | null>(null);
  const [hookNote, setHookNote] = useState<string | null>(null);
  const hookClips = useMemo(() => (hookView ? toClips(hookView) : []), [hookView]);
  const hookLay = useMemo(
    () => (hookView ? layout(hookClips, hookView, hookPicks.extra) : null),
    [hookClips, hookView, hookPicks.extra],
  );
  const selectTake = (takeId: string | null) => {
    setSelectedTakeId(takeId);
    if (takeId) setSelectedHookId(null);
  };
  const selectHook = (pickId: string | null) => {
    setSelectedHookId(pickId);
    if (pickId) setSelectedTakeId(null);
  };
  const changeHook = (clips: TrackClip[]) => void hookPicks.setPicks(clips);
  // Regions never reach past the video's end (a shift-relock or a shorter
  // re-render can leave them there): drawn clamped, saved clamped.
  const gameRegions = view?.session.game_regions;
  const regions = useMemo(
    () => clampRegions(gameRegions ?? [], durationS),
    [gameRegions, durationS],
  );
  const seek = (s: number) => {
    if (video) video.currentTime = s;
  };

  // Record = arm the devices, start the take once the stream is live; the
  // devices are released when the take ends (or permission is refused).
  const startedRef = useRef(false);
  const startCapture = capture.start;
  useEffect(() => {
    if (!armed) {
      startedRef.current = false;
      return;
    }
    if (media.error) {
      setArmed(false);
      return;
    }
    if (media.stream && !startedRef.current) {
      startedRef.current = true;
      startCapture();
    }
  }, [armed, media.stream, media.error, startCapture]);
  useEffect(() => {
    if (startedRef.current && !capture.recording) setArmed(false);
  }, [capture.recording]);

  // Export
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);

  const cameraPreview = useMemo(
    () =>
      cameraOn && media.stream && media.stream.getVideoTracks().length > 0 ? media.stream : null,
    [cameraOn, media.stream],
  );

  const backLink = (
    <>
      <VideoSwitcher kind={target.kind} videoKey={target.key} />
      <Link to="/">
        <Button variant="ghost" size="sm">
          ← home
        </Button>
      </Link>
    </>
  );

  if (narration.session.isError) {
    return (
      <PageShell>
        <PageHeader
          title="Narrate"
          subtitle={`${target.kind} · ${target.key}`}
          trailing={backLink}
        />
        <p className="font-mono text-sm text-danger">
          could not load this video · {(narration.session.error as Error).message}
        </p>
      </PageShell>
    );
  }

  if (!view) {
    return (
      <PageShell>
        <PageHeader
          title="Narrate"
          subtitle={`${target.kind} · ${target.key}`}
          trailing={backLink}
        />
        <p className="font-mono text-sm text-text-muted">loading…</p>
      </PageShell>
    );
  }

  const { session } = view;
  const busy = capture.recording;
  const failed: { error: unknown }[] = [
    narration.editTake,
    narration.splitTake,
    narration.saveMix,
    narration.saveGameRegions,
    narration.removeTake,
  ];
  const failure = failed.find((m) => m.error)?.error;
  const editError = failure ? (failure as Error).message : null;
  const selectedTake = session.takes.find((t) => t.id === selectedTakeId);
  const selectedHook = hookLay?.hooks.find((h) => h.clip.id === selectedHookId);

  return (
    <PageShell>
      <PageHeader
        title="Narrate"
        subtitle={`${target.kind} · ${target.key}`}
        trailing={
          <>
            <Badge tone="muted">{view.game}</Badge>
            {view.stale && <Badge tone="danger">stale</Badge>}
            {hookView && (
              <HookChangesBadge
                hook={hookView}
                clips={hookClips}
                rendered={hookPicks.rendered}
                storyboard={storyboard.data}
                takes={session.takes}
                onChange={changeHook}
                onDiscard={hookPicks.reset}
              />
            )}
            {backLink}
          </>
        }
      />

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-12">
        <div className="space-y-6 lg:col-span-8">
          <div className="relative">
            <VideoPlayer ref={setVideo} src={view.video_url} />
            <CameraBoxEditor
              cameraPx={view.camera_px}
              camera={session.camera}
              preview={cameraPreview}
              onSave={(camera) => narration.saveCamera.mutate(camera)}
            />
          </div>

          <Timeline
            durationS={durationS}
            video={video}
            disabled={busy}
            onSeek={seek}
            takes={session.takes}
            selectedTakeId={selectedTakeId}
            onSelectTake={selectTake}
            onTakeEdit={(takeId, edit) => narration.editTake.mutate({ takeId, edit })}
            buffers={buffers}
            waveforms={waveforms}
            frames={frames.data}
            peaks={peaks.data}
            markers={view.markers}
            regions={regions}
            mix={session.mix}
            onRegionsChange={(next) =>
              narration.saveGameRegions.mutate(clampRegions(next, durationS))
            }
            hook={
              hookView && {
                hook: hookView,
                clips: hookClips,
                extra: hookPicks.extra,
                storyboard: storyboard.data,
                selectedId: selectedHookId,
                onSelect: selectHook,
                onChange: changeHook,
                onUndo: hookPicks.undo,
                onRedo: hookPicks.redo,
                onNote: setHookNote,
              }
            }
          />
          {editError && <p className="font-mono text-xs text-danger">{editError}</p>}
          {(hookPicks.error || hookNote) && (
            <p className="flex items-center gap-2 font-mono text-xs text-danger">
              {hookPicks.error ?? hookNote}
              {hookPicks.stale && (
                <Button size="sm" variant="danger" onClick={hookPicks.reset}>
                  discard
                </Button>
              )}
            </p>
          )}
        </div>

        <aside className="space-y-6 lg:col-span-4">
          <RecordControls
            recording={capture.recording}
            starting={armed && !capture.recording}
            canRecord={!!video && !armed}
            onRecord={() => {
              audio.ensure();
              setArmed(true);
            }}
            checkingMic={checkingMic}
            onCheckMic={setCheckingMic}
            onStop={capture.stop}
            cameraOn={cameraOn}
            onCameraToggle={setCameraOn}
            microphones={devices.microphones}
            cameras={devices.cameras}
            microphoneId={microphoneId}
            cameraId={cameraId}
            onMicrophoneChange={setMicrophoneId}
            onCameraChange={setCameraId}
            level={level}
            streamError={media.error}
            uploading={narration.addTake.isPending}
            uploadError={narration.addTake.error?.message ?? null}
          />

          {hookView && selectedHook && (
            <HookInspector
              key={`${selectedHook.clip.id}:${selectedHook.clip.start_s}:${selectedHook.clip.end_s}`}
              span={selectedHook}
              total={hookLay?.hooks.length ?? 0}
              hook={hookView}
              clips={hookClips}
              extra={hookPicks.extra}
              storyboard={storyboard.data}
              videoKey={target.key}
              onChange={changeHook}
              onClose={() => selectHook(null)}
            />
          )}
          {hookView && !selectedHook && !selectedTake && (
            <StoryboardPanel
              storyboard={storyboard.data}
              hook={hookView}
              clips={hookClips}
              onApply={hookPicks.setPicks}
            />
          )}

          {selectedTake && (
            <TakeInspector
              key={selectedTake.id}
              take={selectedTake}
              video={video}
              busy={busy || narration.splitTake.isPending || narration.removeTake.isPending}
              onEdit={(edit) => narration.editTake.mutate({ takeId: selectedTake.id, edit })}
              onPlay={() => {
                audio.ensure();
                if (!video) return;
                video.currentTime = effectiveStart(selectedTake);
                void video.play();
              }}
              onSplit={(atS) => narration.splitTake.mutate({ takeId: selectedTake.id, atS })}
              onDelete={() => narration.removeTake.mutate(selectedTake.id)}
            />
          )}

          <MixPanel mix={session.mix} onSave={(mix) => narration.saveMix.mutate(mix)} />

          <section className="surface-elevated space-y-3 rounded-lg p-4">
            <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">
              camera box
            </h2>
            <p className="font-mono text-xs text-text-dim">
              Drag the box on the video to move it; drag its corner to resize.
            </p>
            <CameraSettings
              camera={session.camera}
              busy={narration.saveCamera.isPending}
              onSave={(camera) => narration.saveCamera.mutate(camera)}
            />
          </section>

          <SuggestionsPanel target={target} onSeek={seek} />

          <ExportPanel
            stale={view.stale}
            takeCount={session.takes.filter((t) => !t.muted).length}
            onRelock={() => narration.relockTakes.mutate(0)}
            suggestedShiftS={view.suggested_shift_s}
            onShiftRelock={(s) => narration.relockTakes.mutate(s)}
            onDeleteAllTakes={() => narration.removeAllTakes.mutate(session.takes.map((t) => t.id))}
            resolvingStale={narration.relockTakes.isPending || narration.removeAllTakes.isPending}
            onExport={() =>
              narration.exportVideo.mutate(false, { onSuccess: ({ job_id }) => setJobId(job_id) })
            }
            starting={narration.exportVideo.isPending}
            job={job.data}
            error={narration.exportVideo.error?.message ?? null}
            narratedUrl={jobId ? narratedVideoUrl(view, jobId) : view.export_url}
          />
        </aside>
      </div>
    </PageShell>
  );
}
