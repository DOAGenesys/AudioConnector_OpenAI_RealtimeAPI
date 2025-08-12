
import asyncio
import time

class RateLimiter:
    def __init__(self, rate_limit, burst_limit, window_seconds=1.0):
        self.rate_limit = rate_limit 
        self.burst_limit = burst_limit
        self.window = window_seconds
        self.timestamps = []
        self.lock = asyncio.Lock()
        self.dynamic_limit = None  # Add dynamic limit from OpenAI
        self.reset_seconds = None

    async def update_limits(self, new_limit, reset_seconds):
        async with self.lock:
            self.dynamic_limit = new_limit
            self.reset_seconds = reset_seconds
            # Adjust burst limit based on new rate limit
            self.burst_limit = min(self.burst_limit, new_limit)

    async def acquire(self):
        async with self.lock:
            now = time.time()
            window_start = now - self.window
            
            # Use dynamic limit if available
            effective_limit = self.dynamic_limit or self.rate_limit
            
            self.timestamps = [ts for ts in self.timestamps if ts > window_start]

            if len(self.timestamps) >= self.burst_limit:
                return False

            if len(self.timestamps) >= effective_limit:
                oldest = self.timestamps[0]
                if now - oldest < self.window:
                    return False

            self.timestamps.append(now)
            return True

    def get_current_rate(self):
        now = time.time()
        window_start = now - self.window
        recent = [ts for ts in self.timestamps if ts > window_start]
        return len(recent) / self.window if recent else 0
