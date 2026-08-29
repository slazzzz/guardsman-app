### PERSISTENT VIEW RESTORATION ###
# Button callbacks (Join Event, drill roster Join/Leave/View Roster, stat/
# badge submission review) don't survive a restart unless the view - with
# its matching custom_id - is re-attached via bot.add_view(). This module is
# the single place that logic lives, so app.py's on_ready (once per process
# start) and /admin_restore_views in bot/cogs/admin.py (on demand, without a
# full restart - e.g. after a hand-edited DB row, or a bad view class picked
# up by /admin_reload_cog) both call the exact same code instead of two
# copies drifting apart.

from bot.client import bot
from bot.config import bot_data
from bot.database import cursor
from bot.ui import BadgeSubmissionReviewView, DrillRosterView, JoinEventView, StatBatchSubmissionReviewView


def restore_join_event_view() -> bool:
    active_event_id = bot_data.get("active_event_data", {}).get("active_event_id")
    if not active_event_id:
        return False
    bot.add_view(JoinEventView(active_event_id))
    return True


def restore_submission_review_views() -> int:
    """Re-attaches a review view per still-pending stat/badge submission.
    COALESCE(batch_id, id) so pre-batch rows (batch_id NULL, from before
    that column existed) each restore as their own single-item batch
    instead of all getting lumped into one NULL group."""
    cursor.execute("""
        SELECT COALESCE(batch_id, id), GROUP_CONCAT(id)
        FROM stat_submissions
        WHERE status = 'pending'
        GROUP BY COALESCE(batch_id, id)
    """)
    stat_batches = [[int(x) for x in ids_csv.split(",")] for _batch_id, ids_csv in cursor.fetchall()]
    for submission_ids in stat_batches:
        bot.add_view(StatBatchSubmissionReviewView(submission_ids))

    cursor.execute("SELECT id FROM badge_submissions WHERE status = 'pending'")
    pending_badge_ids = [row[0] for row in cursor.fetchall()]
    for submission_id in pending_badge_ids:
        bot.add_view(BadgeSubmissionReviewView(submission_id))

    return len(stat_batches) + len(pending_badge_ids)


def restore_drill_views() -> int:
    """Only drills still accepting participants need this - a drill that's
    already in_progress/completed/cancelled had its view removed from the
    message by refresh_drill_message()."""
    cursor.execute("SELECT id FROM drills WHERE status IN ('recruiting', 'ready')")
    active_drill_ids = [row[0] for row in cursor.fetchall()]
    for drill_id in active_drill_ids:
        bot.add_view(DrillRosterView(drill_id))
    return len(active_drill_ids)


def restore_all_views() -> dict:
    """Re-runs every view restoration routine and returns a summary dict of
    how many of each were (re-)attached."""
    return {
        "join_event": restore_join_event_view(),
        "submission_reviews": restore_submission_review_views(),
        "drills": restore_drill_views(),
    }
