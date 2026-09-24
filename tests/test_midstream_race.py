"""
Isolated logic test for the #9 fix.

The log evidence (app_log.txt, 04:51:29,208 -> ,221, two different thread
ids on the paired MUTE GUARD lines) shows: a real channel change calls
play_file_segmented() for the new file; ~13ms later, on what the log shows
is a DIFFERENT thread, the mid-stream check reads app_state["last_video_
play_started_at"] and sees a value old enough (>2000ms) that its guard
doesn't block it -- even though a play for the exact same file is already
in flight. That can only happen if the reader sees the timestamp BEFORE
the real-channel-change thread's own stamp write becomes visible to it
(a classic unsynchronized read/write race on shared app_state).

Rather than trying to guess the exact scheduling that produces that window
(not reproducible without the real app/threads), this test isolates the
one part that IS fully specified and provably true from the log: VLC's
get_media()/get_mrl() do not reflect a play() call until ~250ms later
(measured directly: 04:51:29,208 call -> 04:51:29,474 "OPENED inline"),
while vlc.player.get_state() flips to Opening essentially as soon as
play() is called. The fix adds a check against get_state() that does not
depend on last_video_play_started_at at all, so it closes the duplicate-
open window regardless of whatever caused the timestamp race.
"""

class FakeMedia:
    def __init__(self, mrl):
        self._mrl = mrl
    def get_mrl(self):
        return self._mrl


class FakePlayer:
    """Models the real, log-confirmed timing: state flips to Opening at
    call time; get_media() only catches up ~250ms later (matches the
    04:51:29,208 -> 04:51:29,474 gap in app_log.txt)."""
    def __init__(self):
        self._media = FakeMedia("file:///CH05/Rugrats.avi")
        self._state = "Playing"
        self._pending_media = None
        self._land_media_at_ms = None

    def play_async(self, new_mrl, now_ms, land_after_ms=250):
        self._state = "Opening"              # near-instant, per libVLC's own model
        self._pending_media = FakeMedia(new_mrl)
        self._land_media_at_ms = now_ms + land_after_ms

    def tick(self, now_ms):
        if self._pending_media is not None and now_ms >= self._land_media_at_ms:
            self._media = self._pending_media
            self._pending_media = None
            self._state = "Playing"

    def get_media(self):
        return self._media

    def get_state(self):
        return self._state


def normalize(mrl):
    return mrl.replace("file:///", "").lower()


OPENING, BUFFERING = "Opening", "Buffering"


def mid_stream_check(player, sched_path, last_play_started_at, now_ms,
                      use_state_guard):
    if use_state_guard and player.get_state() in (OPENING, BUFFERING):
        return False
    if now_ms - last_play_started_at <= 2000:
        return False
    loaded_path = normalize(player.get_media().get_mrl())
    if loaded_path and loaded_path != normalize(sched_path):
        return True
    return False


def run_scenario(label, use_state_guard):
    player = FakePlayer()
    t0 = 1000
    # Model the race explicitly: the timestamp guard has ALREADY failed to
    # protect us (this is exactly the log's observed precondition -- the
    # concurrent reader saw a stale, >2000ms-old timestamp). What's left to
    # test is whether the NEW, timestamp-independent state guard catches it.
    stale_timestamp = t0 - 5000
    player.play_async("file:///CH06/101_Dalmatians.avi", t0, land_after_ms=250)

    t_check = t0 + 13  # matches the log's 13ms gap
    player.tick(t_check)
    fired = mid_stream_check(player, "CH06/101_Dalmatians.avi",
                              stale_timestamp, t_check, use_state_guard)
    print(f"{label}: duplicate fires = {fired}")
    return fired


def run_real_drift_scenario(use_state_guard):
    """A genuine drift (nothing in flight, VLC has been sitting on the
    wrong file for a while) must still be caught -- the fix must not mask
    real bugs."""
    player = FakePlayer()
    player._media = FakeMedia("file:///CH05/Rugrats.avi")
    player._state = "Playing"
    now_ms = 10000
    fired = mid_stream_check(player, "CH06/101_Dalmatians.avi", 0, now_ms,
                              use_state_guard)
    print(f"Real drift (should still fire) -> fired = {fired}")
    return fired


def _run_all():
    print("=== Reproducing the log's failure mode: timestamp guard already stale ===")
    before = run_scenario("BEFORE FIX (no state guard)", use_state_guard=False)
    after = run_scenario("AFTER FIX (state guard added)", use_state_guard=True)

    print()
    print("=== Real drift must still be detected ===")
    real_drift = run_real_drift_scenario(use_state_guard=True)

    print()
    assert before is True, "sanity check: bug must reproduce without the fix"
    assert after is False, "fix must block the duplicate even with a stale timestamp"
    assert real_drift is True, "fix must not mask genuine drift"
    print("ALL CHECKS PASSED: the state guard blocks the duplicate open even when")
    print("the timestamp guard has already been defeated by the race, and it does")
    print("not suppress a genuine, real content mismatch.")


if __name__ == "__main__":
    from extract import run_logged  # also saves this run's output to root/temp, next to app_log.txt
    run_logged(__file__, _run_all)
else:
    _run_all()  # imported (e.g. by pytest): the checks still run at collection, as before
