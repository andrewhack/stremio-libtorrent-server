"""On-the-fly HLS transcoder: one ffmpeg job per request id, fMP4 segments on disk.

`build_hls_cmd` is pure (unit-testable); `Converter` manages the ffmpeg subprocess lifecycle.
Uses an EVENT playlist so the player can start after the first segment instead of waiting for the
whole transcode (full VOD-on-demand seeking is a later refinement).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from stremiosrv import metrics

logger = logging.getLogger("stremiosrv.transcode")


def build_hls_cmd(media_url: str, decision: dict, profile: str | None, out_dir: str | Path) -> list[str]:
    out_dir = str(out_dir)
    v = decision.get("video", {})
    a = decision.get("audio")
    argv = ["ffmpeg", "-hide_banner", "-y"]

    if v.get("action") == "transcode":
        if profile == "nvenc-linux":
            argv += ["-hwaccel", "cuda"]
        elif profile and profile.startswith("vaapi"):
            argv += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]

    argv += ["-i", media_url, "-map", "0:v:0"]
    if a is not None:
        argv += ["-map", "0:a:0?"]

    # Video
    if v.get("action") == "copy":
        argv += ["-c:v", "copy"]
    else:
        w = v.get("scale_width")
        if profile == "nvenc-linux":
            argv += ["-vf", f"scale={w}:-2:flags=lanczos,format=yuv420p" if w else "format=yuv420p",
                     "-c:v", "h264_nvenc", "-preset", "p4"]
        elif profile and profile.startswith("vaapi"):
            if w:
                argv += ["-vf", f"scale_vaapi=w={w}:h=-2"]
            argv += ["-c:v", "h264_vaapi"]
        else:
            if w:
                argv += ["-vf", f"scale={w}:-2:flags=lanczos"]
            argv += ["-c:v", "libx264", "-preset", "veryfast"]

    # Audio
    if a is not None:
        if a.get("action") == "copy":
            argv += ["-c:a", "copy"]
        else:
            argv += ["-c:a", "aac", "-ac", "2", "-ab", "384000"]

    argv += [
        "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event",
        "-hls_segment_type", "fmp4", "-hls_flags", "independent_segments",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", f"{out_dir}/seg%d.m4s",
        "-master_pl_name", "master.m3u8", f"{out_dir}/index.m3u8",
    ]
    return argv


class Converter:
    # How long to wait for a terminated ffmpeg to actually exit before killing it and reclaiming its
    # directory. Short on purpose: SIGTERM ends an encode promptly, and the alternative is holding a
    # request open behind a wedged child.
    STOP_TIMEOUT = 5.0

    def __init__(self, cache_root: str, profile: str | None):
        self.base = Path(cache_root) / "transcode"
        self.profile = profile
        self._jobs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def job_dir(self, job_id: str) -> Path:
        """Directory holding one job's HLS output.

        `job_id` arrives straight from the URL and now selects a tree to DELETE, so it is validated
        here — one chokepoint — rather than at each call site. The separator and dot checks are what
        make this robust to the encodings that survive client-side normalisation: `%2e%2e` reaches
        the route as a literal `..`, and because the transcode base sits directly under the cache
        root, `/hlsv2/%2e%2e/certificates.pem` resolved onto the server's TLS private key and
        returned it with a 200. The resolve() comparison is defence in depth for symlinked roots.
        """
        if not job_id or job_id in (".", "..") or "/" in job_id or chr(92) in job_id:
            raise ValueError(f"unsafe transcode job id: {job_id!r}")
        candidate = self.base / job_id
        try:
            if candidate.resolve().parent != self.base.resolve():
                raise ValueError(f"unsafe transcode job id: {job_id!r}")
        except OSError as e:
            raise ValueError(f"unresolvable transcode job id: {job_id!r}") from e
        return candidate

    def job_file(self, job_id: str, filename: str) -> Path:
        """One artefact inside a job directory (playlist, init segment, or media segment).

        `filename` is the same hole from the other end: a job id can be perfectly well-formed while
        the file name walks out of the directory it names.
        """
        if not filename or filename in (".", "..") or "/" in filename or chr(92) in filename:
            raise ValueError(f"unsafe transcode file name: {filename!r}")
        d = self.job_dir(job_id)
        candidate = d / filename
        try:
            if candidate.resolve().parent != d.resolve():
                raise ValueError(f"unsafe transcode file name: {filename!r}")
        except OSError as e:
            raise ValueError(f"unresolvable transcode file name: {filename!r}") from e
        return candidate

    def _remove_job_dir(self, job_id: str) -> None:
        try:
            d = self.job_dir(job_id)
        except ValueError:
            return  # never delete a tree an unsafe id named; the route rejects it separately
        shutil.rmtree(d, ignore_errors=True)

    def ensure_job(self, job_id: str, media_url: str, decision: dict) -> Path:
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None and existing.poll() is None:
                return self.job_dir(job_id)
            d = self.job_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            argv = build_hls_cmd(media_url, decision, self.profile, d)
            # Not a context manager on purpose: this handle IS ffmpeg's stderr for the lifetime
            # of the child process, so closing it at the end of a with-block would truncate the
            # job's log the moment it starts writing. Released when the Popen is collected.
            log = open(d / "ffmpeg.log", "wb")  # noqa: SIM115
            self._jobs[job_id] = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log)
            # Counted here, not in the route: the early return above means a player re-fetching
            # master.m3u8 mid-playback reuses this job, and counting requests would multiply it.
            metrics.record_hls_session(decision)
            return d

    def active_count(self) -> int:
        """Number of ffmpeg transcode jobs currently running (for the admin transcode/GPU card)."""
        with self._lock:
            return sum(1 for p in self._jobs.values() if p.poll() is None)

    def _end(self, p: subprocess.Popen | None) -> None:
        """Terminate a job's ffmpeg and wait for it to actually go away.

        The wait is the point. ffmpeg creates a new file per segment, so removing the tree while the
        child is still alive races it and can leave freshly-written segments behind — which is the
        very leak this code exists to close.
        """
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=self.STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()

    def stop(self, job_id: str) -> None:
        """End a job and reclaim its disk.

        The directory goes even when no process is tracked: after a restart the handles are gone but
        the segments are not, and a client calling /destroy on a job this process never started is
        the only signal we will ever get that its output is finished with.
        """
        with self._lock:
            p = self._jobs.pop(job_id, None)
        self._end(p)
        self._remove_job_dir(job_id)

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.items())
            self._jobs.clear()
        for job_id, p in jobs:
            self._end(p)
            self._remove_job_dir(job_id)

    def sweep(self, max_age: float = 0.0) -> int:
        """Delete job directories that no live ffmpeg owns. Returns how many were removed.

        Two callers, two ages. At startup it runs with `max_age=0`: this process owns nothing yet,
        so every directory present belongs to a run that is already gone. The periodic caller passes
        a grace, so a job merely between segment writes is never mistaken for garbage.

        A sweep is needed because `/destroy` cannot be the only reclaim path — a killed TV app, a
        dropped connection and a container restart all skip it. One directory on a real box outlived
        two months and two restarts holding 173 files, because nothing ever swept.
        """
        try:
            names = os.listdir(self.base)
        except OSError:
            return 0  # nothing has transcoded yet
        with self._lock:
            live = {jid for jid, p in self._jobs.items() if p.poll() is None}
        now = time.time()
        removed: list[str] = []
        for name in names:
            if name in live:
                continue
            d = self.base / name
            try:
                if not d.is_dir():
                    continue  # only job directories; never a stray file sitting beside them
                # Clamped at zero: `now` is read once before the loop, and a directory
                # stamped a hair later reads as negative age -- which at max_age=0 skips the very
                # sweep that is supposed to take no grace.
                if max(0.0, now - d.stat().st_mtime) < max_age:
                    continue
            except OSError:
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed.append(name)
        if removed:
            with self._lock:
                for name in removed:
                    self._jobs.pop(name, None)
            logger.info("transcode gc: reclaimed %d orphaned job dir(s)", len(removed))
        return len(removed)


def run_transcode_gc(converter: Converter, interval: int = 300, max_age: int = 600) -> None:
    """Background loop reclaiming orphaned transcode output. Runs forever."""
    if not logger.handlers:  # match the evictor: uvicorn doesn't surface our INFO logs by default
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [transcode] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    while True:
        time.sleep(interval)
        try:
            converter.sweep(max_age=max_age)
        except Exception:
            logger.exception("transcode gc pass failed")
