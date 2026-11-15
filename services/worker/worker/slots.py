from __future__ import annotations

from dataclasses import dataclass

from mf_core.queue import ClaimedJob


@dataclass(slots=True)
class Slots:
    cpu_total: int
    gpu_total: int
    cpu_used: int = 0
    gpu_used: int = 0

    @property
    def cpu_free(self) -> int:
        return max(0, self.cpu_total - self.cpu_used)

    @property
    def gpu_free(self) -> int:
        return max(0, self.gpu_total - self.gpu_used)

    def can_fit(self, cpu_cost: int, gpu_sessions: int, requires_gpu: bool) -> bool:
        if cpu_cost > self.cpu_free:
            return False
        if requires_gpu and self.gpu_free <= 0:
            return False
        return gpu_sessions <= self.gpu_free

    def has_free(self) -> bool:
        return self.cpu_free > 0

    def reserve(self, job: ClaimedJob) -> None:
        self.cpu_used += job.cpu_cost
        self.gpu_used += job.gpu_sessions

    def release(self, job: ClaimedJob) -> None:
        self.cpu_used = max(0, self.cpu_used - job.cpu_cost)
        self.gpu_used = max(0, self.gpu_used - job.gpu_sessions)

    def snapshot(self) -> dict[str, int]:
        return {"cpu_total": self.cpu_total, "cpu_used": self.cpu_used,
                "gpu_total": self.gpu_total, "gpu_used": self.gpu_used}