"""Product submissions: a logged-in user proposes a product, an admin reviews it.

Approval itself is approve_submission() in migration 0013, one transaction
called by RPC, which creates a NEW product only and never updates an existing
product or ingredients row. This module validates, stores and reads; the
approve route passes the admin's body to the function as sent.

Every /admin route depends on get_admin_user_id: 401 without a valid login,
403 unless the caller's users row has role 'admin'.

Abuse limits (429): a user may have at most MAX_PENDING_PER_USER submissions
waiting for review, and may upload at most UPLOADS_PER_HOUR images an hour.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from postgrest.exceptions import APIError
from starlette.concurrency import run_in_threadpool

from app.core import cache
from app.core.services import image_upload, submission_service
from app.core.services.ingredient_lookup import fetch_all_rows, load_ingredients
from app.core.services.rate_limit import SlidingWindowLimiter
from app.core.services.rpc_errors import BodyHTTPException, http_error_for_rpc
from app.core.services.token import get_admin_user_id, get_current_user_id
from app.db.connection import supabase
from app.schemas import (SUBMISSION_IMAGE_PATH, ApproveRequest, CleanupImagesRequest, RejectRequest,
                         SubmissionCreate, SubmissionEdit)

router = APIRouter()

STATUSES = ("pending", "approved", "rejected")
# Two foreign keys point product_submissions at users (submitted_by and
# reviewed_by), so the embed names the column it follows.
SUBMITTER_EMBED = "submitter:users!submitted_by(display_name)"

# Enough for a keen user's batch; more than this waiting at once is flooding
# the review queue.
MAX_PENDING_PER_USER = 10
MSG_TOO_MANY_PENDING = (f"You already have {MAX_PENDING_PER_USER} submissions waiting for review. "
                        "You can send another once an admin has reviewed one.")

# Uploads are stored (and public) as soon as they arrive, before any submission
# uses them, so they are rate-limited per user. Counted on every attempt, as
# each one costs a decode. In memory: see app/core/services/rate_limit.py.
UPLOADS_PER_HOUR = 20
upload_limiter = SlidingWindowLimiter(UPLOADS_PER_HOUR, 3600)
MSG_TOO_MANY_UPLOADS = f"Too many image uploads: at most {UPLOADS_PER_HOUR} an hour. Try again later."

MSG_BAD_STORED_IMAGE = ("The submission's image_path is not an uploaded image. "
                        "Save a new photo with PATCH /submissions/admin/{id} first.")


def _not_pending(status: str) -> BodyHTTPException:
    return BodyHTTPException(409, {"detail": f"the submission is {status}, not pending", "code": "SBNPD"})


def _unknown_ingredient_ids(items: List[Dict[str, Any]]) -> List[str]:
    """ingredient_ids in the stored items that are not in the ingredients table."""
    wanted = {item["ingredient_id"] for item in items if item.get("ingredient_id")}
    if not wanted:
        return []
    res = supabase.table("ingredients").select("id").in_("id", sorted(wanted)).execute()
    found = {str(row["id"]) for row in (res.data or []) if str(row["id"]) in wanted}
    return sorted(wanted - found)


def _refuse_unknown_ids(items: List[Dict[str, Any]]) -> None:
    unknown = _unknown_ingredient_ids(items)
    if unknown:
        raise BodyHTTPException(422, {"detail": "unknown ingredient_id", "code": "SBUNK", "details": unknown})


def _load_submission(submission_id: str) -> Dict[str, Any]:
    """The submission row with its submitter's name, or 404. A malformed id is a
    404 too, rather than a Postgres type error answered as 500."""
    try:
        uuid.UUID(submission_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Submission not found")
    res = (supabase.table("product_submissions").select(f"*, {SUBMITTER_EMBED}")
           .eq("id", submission_id).limit(1).execute())
    if not res.data:
        raise HTTPException(status_code=404, detail="Submission not found")
    return res.data[0]


def _review_detail(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = submission_service.merged_payload(row)
    return {
        "id": row["id"],
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "submitter_name": submission_service.submitter_name(row),
        "reviewed_at": row.get("reviewed_at"),
        "review_notes": row.get("review_notes"),
        "product_id": row.get("product_id"),
        "has_edits": bool(row.get("edited_payload")),
        "submission": payload,
        "duplicate_candidates": submission_service.duplicate_candidates(
            payload.get("brand"), payload.get("name"), submission_service.load_products()),
        "ingredients": submission_service.review_ingredients(payload, load_ingredients()),
    }


# --- Logged-in user ------------------------------------------------------------

@router.post("/images", openapi_extra=image_upload.UPLOAD_OPENAPI)
async def upload_submission_image(request: Request, user_id: str = Depends(get_current_user_id)):
    retry_after = upload_limiter.hit(user_id)
    if retry_after is not None:
        raise HTTPException(status_code=429, detail=MSG_TOO_MANY_UPLOADS,
                            headers={"Retry-After": str(retry_after)})
    data, ext, content_type = await image_upload.read_image_upload(request)
    try:
        # Synchronous storage upload (up to its 20 s timeout): off the event loop.
        return await run_in_threadpool(image_upload.store_image, data, ext, content_type, "submissions")
    except Exception as e:
        print("POST /submissions/images error:", e)
        raise HTTPException(status_code=500, detail="Failed to store the image.")


@router.post("", status_code=201)
def create_submission(body: SubmissionCreate, user_id: str = Depends(get_current_user_id)):
    try:
        payload = body.stored_payload()
        # A soft cap: two requests racing could both pass it, which is harmless.
        waiting = (supabase.table("product_submissions").select("id")
                   .eq("submitted_by", user_id).eq("status", "pending")
                   .limit(MAX_PENDING_PER_USER).execute())
        if len(waiting.data or []) >= MAX_PENDING_PER_USER:
            raise HTTPException(status_code=429, detail=MSG_TOO_MANY_PENDING)
        _refuse_unknown_ids(payload["ingredients"])
        res = supabase.table("product_submissions").insert({
            "submitted_by": user_id, "status": "pending", "payload": payload,
        }).execute()
        row = res.data[0]
        return {"id": row["id"], "status": row["status"], "created_at": row["created_at"]}
    except HTTPException:
        raise
    except Exception as e:
        print("POST /submissions error:", e)
        raise HTTPException(status_code=500, detail="Failed to create submission.")


@router.get("/mine")
def get_my_submissions(user_id: str = Depends(get_current_user_id)):
    try:
        rows = fetch_all_rows(lambda: supabase.table("product_submissions")
                              .select("*, product:products(slug)")
                              .eq("submitted_by", user_id).order("created_at", desc=True).order("id"))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        out = []
        for row in rows:
            product = row.get("product") if isinstance(row.get("product"), dict) else None
            out.append({
                "id": row["id"],
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "reviewed_at": row.get("reviewed_at"),
                "review_notes": row.get("review_notes"),
                "product_id": row.get("product_id"),
                "product_slug": product.get("slug") if product else None,
                "summary": submission_service.summary(submission_service.merged_payload(row)),
            })
        return out
    except Exception as e:
        print("GET /submissions/mine error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch your submissions.")


# --- Admin only ----------------------------------------------------------------

@router.get("/admin")
def list_submissions_for_review(
    status: Literal["pending", "approved", "rejected"] = Query("pending"),
    admin_id: str = Depends(get_admin_user_id),
):
    try:
        all_statuses = fetch_all_rows(
            lambda: supabase.table("product_submissions").select("id, status").order("id"))
        counts = {s: sum(1 for r in all_statuses if r.get("status") == s) for s in STATUSES}

        rows = fetch_all_rows(lambda: supabase.table("product_submissions")
                              .select(f"*, {SUBMITTER_EMBED}")
                              .eq("status", status).order("created_at").order("id"))
        rows.sort(key=lambda r: r.get("created_at") or "")
        products = submission_service.load_products()
        items = []
        for row in rows:
            payload = submission_service.merged_payload(row)
            items.append({
                "id": row["id"],
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "submitter_name": submission_service.submitter_name(row),
                "summary": submission_service.summary(payload),
                "flags": submission_service.flags(payload, products),
            })
        return {"counts": counts, "submissions": items}
    except Exception as e:
        print("GET /submissions/admin error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch submissions.")


@router.post("/admin/cleanup-images")
async def cleanup_unused_images(body: Optional[CleanupImagesRequest] = None,
                                admin_id: str = Depends(get_admin_user_id)):
    """Deletes stored photos that nothing uses: uploads under submissions/ and
    products/ last written at least older_than_hours ago (default 24) that no
    product's image_url and no pending submission points at. An approved
    submission's photo counts as used only while the product it created still
    shows it, which also covers products whose photo was replaced before that
    rule existed. Rejected submissions' photos count as unused, which also
    covers those rejected before rejecting deleted the photo. dry_run (the
    default) deletes nothing and reports what would go. No body means the
    defaults.

    Answers {"checked", "unreferenced", "deleted", "failed", "dry_run", "paths"};
    see image_upload.cleanup_unused_uploads. A batch that fails to delete is
    counted in failed, not answered as an error.

    Run by an admin, not on a timer: the code has no scheduler. The timed jobs
    that exist, the notification endpoints, are triggered from outside and
    guarded only by an optional CRON_SECRET (open when it is unset). A button
    behind the admin login is simpler, cannot be called by anyone else, and
    lets the admin see a dry run first."""
    body = body or CleanupImagesRequest()
    try:
        return await run_in_threadpool(image_upload.cleanup_unused_uploads,
                                       body.older_than_hours, body.dry_run)
    except Exception as e:
        print("POST /submissions/admin/cleanup-images error:", e)
        raise HTTPException(status_code=500, detail="Failed to check the stored images.")


@router.get("/admin/{submission_id}")
def get_submission_for_review(submission_id: str, admin_id: str = Depends(get_admin_user_id)):
    try:
        return _review_detail(_load_submission(submission_id))
    except HTTPException:
        raise
    except Exception as e:
        print("GET /submissions/admin/{id} error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch the submission.")


@router.patch("/admin/{submission_id}")
def edit_submission(submission_id: str, body: SubmissionEdit, admin_id: str = Depends(get_admin_user_id)):
    """Saves only the fields sent, on top of earlier edits; the user's payload is
    never changed. An unsent field keeps the user's value."""
    try:
        row = _load_submission(submission_id)
        if row.get("status") != "pending":
            raise _not_pending(row.get("status"))
        edits = body.stored_edits()
        if "ingredients" in edits:
            _refuse_unknown_ids(edits["ingredients"])
        previous = row.get("edited_payload") if isinstance(row.get("edited_payload"), dict) else {}
        old_image = submission_service.merged_payload(row).get("image_path")
        res = (supabase.table("product_submissions")
               .update({"edited_payload": {**previous, **edits}})
               .eq("id", submission_id).eq("status", "pending").execute())
        if not res.data:            # reviewed between the read and the write
            raise _not_pending("no longer pending")
        # A photo replaced by a new upload, or cleared, is deleted once the edit
        # is saved, unless something else still uses it. This submission now
        # points at its new photo, so the check no longer sees it holding the
        # old one. A failure is printed and the edit still answers 200.
        if "image_path" in edits and old_image and old_image != edits["image_path"]:
            image_upload.delete_upload_if_unused(old_image, "PATCH /submissions/admin/{id}")
        return _review_detail(_load_submission(submission_id))
    except HTTPException:
        raise
    except Exception as e:
        print("PATCH /submissions/admin/{id} error:", e)
        raise HTTPException(status_code=500, detail="Failed to save the edits.")


@router.post("/admin/{submission_id}/approve")
def approve_submission(submission_id: str, body: ApproveRequest, admin_id: str = Depends(get_admin_user_id)):
    try:
        row = _load_submission(submission_id)
        image_path = submission_service.merged_payload(row).get("image_path")
        image_url = None
        if image_path:
            # Checked on the way in, and again here, so the product's image_url
            # can only ever be one of our uploads in our bucket.
            if not isinstance(image_path, str) or not SUBMISSION_IMAGE_PATH.fullmatch(image_path):
                raise HTTPException(status_code=422, detail=MSG_BAD_STORED_IMAGE)
            image_url = image_upload.public_url(image_path)
        try:
            res = supabase.rpc("approve_submission", {
                "p_submission_id": submission_id,
                "p_admin_id": admin_id,
                "p_decisions": body.as_sent(),
                "p_image_url": image_url,
            }).execute()
        except APIError as err:
            raise http_error_for_rpc(err, "POST /submissions/admin/{id}/approve")
        finally:
            # Approval adds a product and may add ingredients: drop the cached
            # product tree and ingredient list, whatever the answer was.
            cache.clear()
        return res.data
    except HTTPException:
        raise
    except Exception as e:
        print("POST /submissions/admin/{id}/approve error:", e)
        raise HTTPException(status_code=500, detail="Failed to approve the submission.")


@router.post("/admin/{submission_id}/reject")
def reject_submission(submission_id: str, body: RejectRequest, admin_id: str = Depends(get_admin_user_id)):
    try:
        row = _load_submission(submission_id)
        if row.get("status") != "pending":
            raise _not_pending(row.get("status"))
        image_path = submission_service.merged_payload(row).get("image_path")
        decision = {
            "status": "rejected",
            "review_notes": body.review_notes,
            "reviewed_by": admin_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        if image_path:
            # A rejected submission keeps no photo. Recorded in the same update
            # as the status, by setting edited_payload.image_path to null; the
            # user's payload is never changed. edited_payload's keys replace
            # payload's, so the effective image_path is null from now on:
            #   * GET /submissions/admin/{id} shows "submission": {..., "image_path": null}
            #     and "has_edits": true, and the list's flags.has_photo is false;
            #   * GET /submissions/mine shows no photo at all, so it is unchanged.
            # Recorded even if the deletion below fails or is skipped because
            # another submission or product uses the file: this submission no
            # longer holds it, and the cleanup route deletes it once nothing does.
            previous = row.get("edited_payload") if isinstance(row.get("edited_payload"), dict) else {}
            decision["edited_payload"] = {**previous, "image_path": None}
        res = (supabase.table("product_submissions").update(decision)
               .eq("id", submission_id).eq("status", "pending").execute())
        if not res.data:
            raise _not_pending("no longer pending")
        if image_path:
            # After the status change, so this submission, now rejected, no
            # longer counts as using the photo. A failure is printed; the reject
            # still answers 200.
            image_upload.delete_upload_if_unused(image_path, "POST /submissions/admin/{id}/reject")
        done = res.data[0]
        return {"id": done["id"], "status": done["status"],
                "reviewed_at": done.get("reviewed_at"), "review_notes": done.get("review_notes")}
    except HTTPException:
        raise
    except Exception as e:
        print("POST /submissions/admin/{id}/reject error:", e)
        raise HTTPException(status_code=500, detail="Failed to reject the submission.")
