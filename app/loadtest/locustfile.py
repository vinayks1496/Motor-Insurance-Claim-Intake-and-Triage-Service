from __future__ import annotations

import itertools
import random
import time
from uuid import uuid4

from locust import HttpUser, between, task

SCENARIOS = itertools.cycle(
    [
        ("collision", "private", "A car struck a parking barrier and the rear bumper is damaged.", 48000),
        ("theft", "private", "The insured car was stolen overnight and the owner has informed police.", None),
        ("collision", "delivery", "During paid food delivery the car hit a divider and damaged its front bumper.", 57000),
        ("third_party", "private", "Another vehicle hit the insured car at a junction and one passenger was injured.", 94000),
    ]
)


class ClaimFlowUser(HttpUser):
    wait_time = between(1.0, 2.5)

    @task
    def submit_and_poll_claim(self) -> None:
        kind, vehicle_use, description, estimate = next(SCENARIOS)
        reference = f"LOAD-{uuid4().hex[:16]}"
        payload = {
            "client_reference": reference,
            "policy_number": f"LOAD/2026/{uuid4().hex[:8].upper()}",
            "registration_number": f"KA03MN{random.randint(1000, 9999)}",
            "loss_date": "2026-05-10",
            "reported_at": "2026-05-10",
            "loss_description": description,
            "loss_kind": kind,
            "vehicle_use": vehicle_use,
            "channel": "web",
            "estimate_inr": estimate,
            "injury_reported": kind == "third_party",
            "injury_count": 1 if kind == "third_party" else 0,
            "police_reported": kind == "theft",
        }
        with self.client.post(
            "/api/v1/runs",
            json=payload,
            headers={"Idempotency-Key": f"load-{reference}"},
            name="POST /api/v1/runs",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"submission returned {response.status_code}: {response.text[:160]}")
                return
            run_id = response.json().get("run_id")
            response.success()
        if not run_id:
            return

        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            with self.client.get(
                f"/api/v1/runs/{run_id}",
                name="GET /api/v1/runs/{run_id}",
                catch_response=True,
            ) as poll:
                if poll.status_code != 200:
                    poll.failure(f"poll returned {poll.status_code}")
                    return
                status = poll.json().get("status")
                if status in {"completed", "escalated", "quarantined"}:
                    poll.success()
                    return
                poll.success()
            time.sleep(1.5)
