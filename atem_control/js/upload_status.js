/* upload_status.js — what a drag-and-drop upload's slot says, worked out
 * from what the server last reported and nothing else.
 *
 * Pure functions (no DOM, no store, no clock of their own: `now` is
 * passed in), so the wording can be read and checked on its own. The
 * server sends `media_pool_upload_progress` ({slot, stage, percent}) each
 * time an upload really moves; media_pool.js keeps, per slot,
 * {stage, percent, movedAt} and asks these for the ring and the words.
 *
 * Stages, in order: (none yet: the POST was accepted), connecting,
 * preparing, waiting (for the slot to go off air), sending (with the
 * percent of the still the switcher has granted), verifying; then
 * `done` locally, for the moment the ring shows full before it clears.
 */

// Nothing new from an upload for this long: say so, plainly.
export const STALL_AFTER_S = 10;
// Nothing at all from the server for this long: stop waiting and say the
// outcome is unknown. Well past the server's own no-progress limits (90 s
// for a transfer, 60 s for a connect), which end the upload with a reason.
export const LOST_AFTER_S = 180;

// The ring's fill in percent, or null for "don't know how far": the ring
// then spins instead of showing a number. Only `sending` has a real
// percent. Held below 100 until the server says the upload ended: the
// last chunk queued is not the switcher's word that it has the still.
export function ringPercent(st) {
    if (st && st.stage === 'done') return 100;
    if (!st || st.stage !== 'sending' || typeof st.percent !== 'number') return null;
    return Math.max(0, Math.min(99, st.percent));
}

// Seconds since the upload last moved (whole seconds, never negative).
export function stalledSeconds(st, now) {
    if (!st || typeof st.movedAt !== 'number') return 0;
    return Math.max(0, Math.floor((now - st.movedAt) / 1000));
}

// The short line under the ring.
export function uploadLabel(st, now) {
    if (st && st.stage === 'done') return 'Done';
    const quiet = stalledSeconds(st, now);
    if (quiet >= STALL_AFTER_S) return `No progress for ${quiet} s`;
    switch (st && st.stage) {
        case 'connecting': return 'Connecting…';
        case 'preparing': return 'Preparing…';
        case 'waiting': return 'Waiting for off air…';
        case 'sending': return `${Math.round(ringPercent(st) || 0)}%`;
        case 'verifying': return 'Checking…';
        default: return 'Starting…';
    }
}

// The full sentence once an upload has been quiet for STALL_AFTER_S,
// else ''. Says what it is waiting on and that it is still trying.
export function uploadStallText(slotIndex, st, now) {
    const quiet = stalledSeconds(st, now);
    if (quiet < STALL_AFTER_S) return '';
    const slot = `Slot ${slotIndex + 1}`;
    switch (st && st.stage) {
        case 'connecting':
            return `${slot}: still connecting to the switcher after ${quiet} s. Still trying…`;
        case 'preparing':
            return `${slot}: still preparing the image after ${quiet} s…`;
        case 'waiting':
            return `${slot}: waiting ${quiet} s so far for the slot to go off air…`;
        case 'sending':
            return `${slot}: no progress for ${quiet} s: the switcher isn't acknowledging. Still trying…`;
        case 'verifying':
            return `${slot}: the switcher has the image; no confirmation yet after ${quiet} s…`;
        default:
            return `${slot}: no word from the upload for ${quiet} s. Still waiting…`;
    }
}

// What changed on the switcher, from the server's `changed`: false = the
// slot keeps its old image, null = unknown. A host's own failure message
// may not say; it has always meant "not changed".
export function changedText(changed) {
    if (changed === false || changed === undefined) {
        return 'Nothing was changed on the switcher: the slot keeps its old image.';
    }
    return 'The switcher may or may not have the new image: check the slot.';
}

// The sentence for a failed upload: the server's reason, then what changed.
export function uploadFailureText(slotIndex, reason, changed) {
    const why = String(reason || 'failed').trim().replace(/[.\s]+$/, '');
    return `Slot ${slotIndex + 1}: upload failed: ${why}. ${changedText(changed)}`;
}

// Profile save's media-pool capture: the sentence once no slot has
// finished for STALL_AFTER_S, else ''. `progress` is the last
// profile_capture_progress ({completed, total, at}). It counts whole
// slots only (a slot's own download has no progress the page hears of),
// so it says "no slot finished", not "nothing moved".
export function captureStallText(progress, now) {
    if (!progress || !(progress.total > 0) || progress.completed >= progress.total) return '';
    if (typeof progress.at !== 'number') return '';
    const quiet = Math.max(0, Math.floor((now - progress.at) / 1000));
    if (quiet < STALL_AFTER_S) return '';
    return `No slot has finished for ${quiet} s: the switcher is slow to send, ` +
           'or not answering. Still trying…';
}
