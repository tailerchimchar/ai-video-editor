import { useQuery } from "@tanstack/react-query";
import { getJob, type JobStatus } from "@/api/assets";

const POLL_MS = 1500;

const isDone = (job: JobStatus | undefined) =>
  job?.status === "completed" || job?.status === "failed";

/** Poll `/api/v1/jobs/{id}` until the job completes or fails. Idle when `jobId` is null. */
export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId as string),
    enabled: jobId !== null,
    refetchInterval: (query) => (isDone(query.state.data) ? false : POLL_MS),
  });
}
