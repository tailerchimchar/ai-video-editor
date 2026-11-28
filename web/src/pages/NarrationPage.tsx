import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { narratedVideoUrl } from "@/api/narration";
import { CameraBoxEditor } from "@/components/narration/CameraBoxEditor";
import { CameraSettings } from "@/components/narration/CameraSettings";
import { ExportPanel } from "@/components/narration/ExportPanel";
import { RecordControls } from "@/components/narration/RecordControls";
import { SuggestionsPanel } from "@/components/narration/SuggestionsPanel";
import { TakeTimeline } from "@/components/narration/TakeTimeline";
import { VideoPlayer } from "@/components/VideoPlayer";
import { VideoSwitcher } from "@/components/VideoSwitcher";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";
import { useAudioContext } from "@/hooks/useAudioContext";
import { useAudioLevel } from "@/hooks/useAudioLevel";
import { useJob } from "@/hooks/useJob";
import { useMediaDevices } from "@/hooks/useMediaDevices";
import { useMediaStream } from "@/hooks/useMediaStream";
import { useMixPreview } from "@/hooks/useMixPreview";
import { useNarrationSession } from "@/hooks/useNarrationSession";
import { useTakeBuffers } from "@/hooks/useTakeBuffers";
import { useTakeCapture } from "@/hooks/useTakeCapture";
import { useVideoClock } from "@/hooks/useVideoClock";
import type { TargetRef } from "@/types/narration";

/**
 * `/narrate/:kind/:key` — record voice (and optionally camera) takes over
 * a finished video, place the camera box, export `narrated.mp4`.
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
  const clock = useVideoClock(video);
  const capture = useTakeCapture({
    video,
    stream: media.stream,
    onTake: (take) => narration.addTake.mutate(take),
  });
  const audio = useAudioContext();
  const { buffers } = useTakeBuffers(view, audio.ctx);
  useMixPreview({ video, session: view?.session, buffers, enabled: !capture.recording, audio });

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

  return (
    <PageShell>
      <PageHeader
        title="Narrate"
        subtitle={`${target.kind} · ${target.key}`}
        trailing={
          <>
            <Badge tone="muted">{view.game}</Badge>
            {view.stale && <Badge tone="danger">stale</Badge>}
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

          <TakeTimeline
            takes={session.takes}
            durationS={clock.duration}
            currentTimeS={clock.currentTime}
            disabled={busy}
            deletingId={
              narration.removeTake.isPending ? (narration.removeTake.variables ?? null) : null
            }
            onPlay={(take) => {
              if (!video) return;
              video.currentTime = take.start_s;
              void video.play();
            }}
            onDelete={(takeId) => narration.removeTake.mutate(takeId)}
            onSeek={(s) => {
              if (video) video.currentTime = s;
            }}
          />
        </div>

        <aside className="space-y-6 lg:col-span-4">
          <RecordControls
            recording={capture.recording}
            starting={armed && !capture.recording}
            canRecord={!!video && !armed}
            onRecord={() => setArmed(true)}
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

          <SuggestionsPanel
            target={target}
            onSeek={(s) => {
              if (video) video.currentTime = s;
            }}
          />

          <ExportPanel
            stale={view.stale}
            takeCount={session.takes.length}
            onRelock={() => narration.relockTakes.mutate(0)}
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
