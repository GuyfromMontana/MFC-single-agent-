# NOTE: --workers 1 is REQUIRED. main.py holds a per-process `_call_cache`
# that glues `call_inbound` state to `call_ended`. Under >1 workers, calls
# that start on one worker and end on another miss the cache and fall back
# to a full Zep re-lookup. Bump to Redis-backed cache before scaling out.
web: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
