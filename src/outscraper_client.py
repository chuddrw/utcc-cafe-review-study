from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.budget import BudgetError, BudgetLedger
from src.logging_util import setup_logging
from src.paths import RAW_DIR

logger = setup_logging()


class OutscraperError(RuntimeError):
    pass


class OutscraperClient:
    def __init__(self, api_key: str, config: dict[str, Any], budget: BudgetLedger | None = None):
        self.api_key = api_key
        self.config = config
        self.budget = budget
        self.base = config["outscraper"]["base_url"].rstrip("/")
        self.client = httpx.Client(timeout=120.0, headers={"X-API-KEY": api_key})

    def close(self) -> None:
        self.client.close()

    def _request(self, path: str, params: list[tuple[str, str]], async_mode: bool = True) -> dict[str, Any]:
        params = list(params) + [("async", "true" if async_mode else "false")]
        url = f"{self.base}{path}"
        try:
            resp = self.client.get(url, params=params)
        except httpx.HTTPError as e:
            raise OutscraperError(f"Network error talking to Outscraper: {e}") from e
        if resp.status_code == 401:
            raise OutscraperError("Invalid Outscraper API key")
        if resp.status_code == 402:
            raise OutscraperError("Outscraper billing/payment method required or past due")
        if resp.status_code >= 400:
            raise OutscraperError(f"Outscraper HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code == 202 or (resp.status_code == 200 and resp.json().get("status") == "Pending"):
            payload = resp.json()
            job_id = payload.get("id")
            if not job_id:
                raise OutscraperError("Async job missing id")
            return self._poll_job(job_id)
        if resp.status_code == 204:
            raise OutscraperError("Outscraper request failed with no results")
        return resp.json()

    def _poll_job(self, job_id: str) -> dict[str, Any]:
        runtime = self.config["runtime"]
        delay = float(runtime["poll_initial_seconds"])
        max_delay = float(runtime["poll_max_seconds"])
        deadline = time.time() + 30 * 60
        url = f"{self.base}/requests/{job_id}"
        started = time.time()
        last_print = 0.0
        logger.info("Waiting on Outscraper job %s", job_id)
        while time.time() < deadline:
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as e:
                logger.warning("Poll network error for %s: %s", job_id, e)
                time.sleep(delay)
                delay = min(delay * 1.5, max_delay)
                continue
            if resp.status_code == 202:
                if time.time() - last_print > 20:
                    msg = f"[outscraper] still waiting on job {job_id[:8]}… ({int(time.time()-started)}s)"
                    print(msg, flush=True)
                    logger.info(msg)
                    last_print = time.time()
                time.sleep(delay)
                delay = min(delay * 1.5, max_delay)
                continue
            if resp.status_code == 204:
                raise OutscraperError(f"Job {job_id} failed")
            if resp.status_code >= 400:
                raise OutscraperError(f"Poll HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            status = str(data.get("status", "")).lower()
            if status in {"success", "completed", "done"} or (
                status not in {"pending", "running", "processing"} and data.get("data") is not None
            ):
                self._cache_raw(job_id, data)
                logger.info("Outscraper job %s finished status=%s", job_id, status or "has_data")
                return data
            if status in {"failure", "failed", "error"}:
                raise OutscraperError(f"Job {job_id} failed: {data}")
            if time.time() - last_print > 20:
                msg = f"[outscraper] job {job_id[:8]} status={status or 'unknown'} ({int(time.time()-started)}s)"
                print(msg, flush=True)
                logger.info(msg)
                last_print = time.time()
            time.sleep(delay)
            delay = min(delay * 1.5, max_delay)
        raise OutscraperError(f"Timed out waiting for job {job_id}")

    def _cache_raw(self, job_id: str, data: dict[str, Any]) -> Path:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        path = RAW_DIR / f"outscraper_{job_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def maps_search(self, queries: list[str], limit: int | None = None) -> list[dict[str, Any]]:
        limit = limit if limit is not None else int(self.config["outscraper"]["search_limit_per_query"])
        params: list[tuple[str, str]] = [("limit", str(limit)), ("language", self.config["outscraper"]["language"]), ("region", self.config["outscraper"]["region"])]
        for q in queries:
            params.append(("query", q))
        reserve_id = None
        est = 0.0
        if self.budget is not None:
            est = self.budget.estimate_outscraper_cad(business_units=float(limit * len(queries)))
            reserve_id = self.budget.reserve(
                kind="outscraper_business",
                provider="outscraper",
                units=float(limit * len(queries)),
                estimated_cad=est,
                note=f"maps_search n_queries={len(queries)} limit={limit}",
            )
        try:
            try:
                payload = self._request("/google-maps-search", params, async_mode=True)
            except OutscraperError:
                payload = self._request("/maps/search", params, async_mode=True)
            places = self._flatten_places(payload.get("data"))
            if self.budget is not None and reserve_id is not None:
                actual = self.budget.estimate_outscraper_cad(business_units=float(len(places)))
                # finalize against previously unused free tier using recorded units = returned places
                self.budget.release(reserve_id)
                self.budget.record_direct(
                    kind="outscraper_business",
                    provider="outscraper",
                    units=float(len(places)),
                    estimated_cad=actual,
                    actual_cad=actual,
                    note=f"maps_search returned={len(places)}",
                    job_id=str(payload.get("id") or ""),
                )
            return places
        except Exception:
            if self.budget is not None and reserve_id is not None:
                self.budget.release(reserve_id)
            raise

    def maps_reviews(
        self,
        query: str,
        *,
        reviews_limit: int,
        cutoff_ts: int | None = None,
        last_pagination_id: str | None = None,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("query", query),
            ("reviewsLimit", str(reviews_limit)),
            ("sort", "newest"),
            ("language", self.config["outscraper"]["language"]),
            ("region", self.config["outscraper"]["region"]),
            ("ignoreEmpty", "false"),
        ]
        if cutoff_ts is not None:
            params.append(("cutoff", str(int(cutoff_ts))))
        if last_pagination_id:
            params.append(("lastPaginationId", last_pagination_id))
        reserve_id = None
        if self.budget is not None:
            est = self.budget.estimate_outscraper_cad(review_units=float(reviews_limit))
            reserve_id = self.budget.reserve(
                kind="outscraper_review",
                provider="outscraper",
                units=float(reviews_limit),
                estimated_cad=est,
                note=f"reviews limit={reviews_limit}",
            )
        try:
            # Endpoint documented as /google-maps-reviews; some clients also use /maps/reviews
            try:
                payload = self._request("/google-maps-reviews", params, async_mode=True)
            except OutscraperError:
                payload = self._request("/maps/reviews", params, async_mode=True)
            reviews = []
            place = {}
            data = payload.get("data")
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, list) and first:
                    place = first[0] if isinstance(first[0], dict) else {}
                elif isinstance(first, dict):
                    place = first
                reviews = place.get("reviews_data") or []
            if self.budget is not None and reserve_id is not None:
                self.budget.release(reserve_id)
                actual = self.budget.estimate_outscraper_cad(review_units=float(len(reviews)))
                self.budget.record_direct(
                    kind="outscraper_review",
                    provider="outscraper",
                    units=float(len(reviews)),
                    estimated_cad=actual,
                    actual_cad=actual,
                    note=f"reviews returned={len(reviews)}",
                    job_id=str(payload.get("id") or ""),
                )
            return {"place": place, "reviews": reviews, "raw": payload}
        except Exception:
            if self.budget is not None and reserve_id is not None:
                self.budget.release(reserve_id)
            raise

    @staticmethod
    def _flatten_places(data: Any) -> list[dict[str, Any]]:
        places: list[dict[str, Any]] = []
        if data is None:
            return places
        if isinstance(data, dict):
            data = [data]
        for item in data:
            if isinstance(item, list):
                for place in item:
                    if isinstance(place, dict):
                        places.append(place)
            elif isinstance(item, dict):
                places.append(item)
        return places

    def auth_probe(self) -> dict[str, Any]:
        """Minimal billable/auth check: 1-place search with limit 1."""
        return {"places": self.maps_search(["Starbucks, Toronto, ON"], limit=1)}
