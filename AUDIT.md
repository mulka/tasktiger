# TaskTiger Security & Bug Audit

**Branch:** `audit/bug-security-review`
**Version:** 0.25.0
**Date:** 2026-03-20

---

## Summary

This audit covers the core Python source files and Lua scripts in the `tasktiger/` package. Issues are grouped by severity.

---

## HIGH — Bugs

### H1. Uninitialized variable in `tasks_from_queue` when task data is missing
**Files:** `tasktiger/task.py:548–568`, `tasktiger/task.py:570–581`

In both branches of `tasks_from_queue`, when `serialized_data is None` and `include_not_found=False`, the local variable `data` is never assigned in that loop iteration. On the first missing task, this raises an `UnboundLocalError`. On subsequent iterations, `data` silently retains the previous task's data, and the wrong `Task` object is appended to the results list.

```python
# task.py:551-566 (load_executions branch)
for idx, serialized_data, serialized_executions, ts in zip(...):
    if serialized_data is None:
        if include_not_found:
            data = {"id": items[idx][0]}
        # else: `data` is never set — UnboundLocalError or stale value
    else:
        data = json.loads(serialized_data)
        executions = [json.loads(e) for e in serialized_executions if e]
    task = Task(tiger, queue=queue, _data=data, ...)  # uses uninitialized `data`
    tasks.append(task)
```

The same pattern appears at lines 573–581 in the non-`load_executions` branch. The `tasks` list always has a `Task` appended regardless of whether `data` was safely set.

---

## HIGH — Security

### H2. Arbitrary code execution via Redis-controlled task function names
**File:** `tasktiger/_internal.py:63–71`

`import_attribute()` dynamically imports any Python module and attribute based on a dotted/colon-separated name string retrieved from Redis:

```python
def import_attribute(name: str) -> Any:
    sep = ":" if ":" in name else "."
    module_name, attribute = name.rsplit(sep, 1)
    module = importlib.import_module(module_name)
    return operator.attrgetter(attribute)(module)
```

The `func` field of task data is stored in Redis (e.g., `t:task:<task_id>`) and read back at execution time (`task.py:274`, `worker.py:481`). An attacker with write access to Redis can set `func` to any importable Python path (e.g., `os:system`, `subprocess:check_output`) and control `args`/`kwargs` to achieve arbitrary code execution. No validation or allowlist is applied to the function name.

This is an inherent architectural issue: the attack surface is Redis. Mitigations include Redis authentication, network-level access controls, and TLS (see H3).

### H3. Redis connection has no TLS and password is passed as a positional argument
**File:** `tasktiger/tasktiger.py:735–736`

```python
conn = redis.Redis(
    host, int(port or 6379), int(db or 0), password, decode_responses=True
)
```

The connection uses no TLS, so all task data, credentials, and tracebacks (which may contain secrets) are transmitted in plaintext. Additionally, `password` is passed as a positional argument, which silently breaks if the `redis-py` API signature ever changes; it is better to use the keyword form `password=password`.

---

## MEDIUM — Bugs

### M1. TOCTOU race condition in `max_queue_size` enforcement
**File:** `tasktiger/task.py:394–399`

The queue-size check is not atomic:

```python
if max_queue_size:
    queue_size = tiger.get_total_queue_size(self.queue)  # separate Redis round-trip
    if queue_size >= max_queue_size:
        raise QueueFullException(...)
```

Multiple concurrent callers can all read the same queue size, all pass the check, and all enqueue — causing the actual queue size to exceed `max_queue_size`. The check and the `ZADD` need to be in the same Lua script or pipeline with an `WATCH`/`MULTI`/`EXEC` transaction to be safe.

### M2. Falsy check silently discards `hard_timeout=0`
**File:** `tasktiger/task.py:151`

```python
if hard_timeout:
    task["hard_timeout"] = hard_timeout
```

`hard_timeout=0` is falsy and is therefore not stored. Workers will fall back to the global `DEFAULT_HARD_TIMEOUT` instead of the intended value. The check should be `if hard_timeout is not None`.

### M3. Falsy `when` value bypasses `_move` timestamp
**File:** `tasktiger/task.py:333`

```python
scripts.move_task(
    ...
    when=when or time.time(),
    ...
)
```

If `when=0.0` (Unix epoch) is intentionally passed, the expression evaluates to `time.time()` because `0.0` is falsy. The correct guard is `when if when is not None else time.time()`.

### M4. `worker_group_name` ignores `single_worker_queues`
**File:** `tasktiger/worker.py:127–131`

```python
self.worker_group_name = hashlib.sha256(
    json.dumps([sorted(self.only_queues), sorted(self.exclude_queues)]).encode("utf8")
).hexdigest()
```

`single_worker_queues` is excluded from the hash. Two workers with the same `only_queues`/`exclude_queues` but different `single_worker_queues` share the same group name and therefore the same scheduled-task lock. This can cause a worker with a broader `single_worker_queues` set to suppress task scheduling that the other worker should perform.

### M5. Falsy normalization of `args`/`kwargs` silently drops data
**File:** `tasktiger/task.py:124–125`

```python
args = args or []
kwargs = kwargs or {}
```

If a caller passes `args=()` (empty tuple) or `kwargs={}` (empty dict), these are replaced with `[]` and `{}` respectively, which is benign. However, any other falsy value (e.g., `args=0`, `args=False`) would be silently replaced by `[]`, discarding the caller's intent. The guard should test `if args is None` to be safe.

---

## MEDIUM — Security

### M6. Task arguments and tracebacks logged and stored in Redis
**Files:** `tasktiger/executor.py:168`, `tasktiger/worker.py:231–236`, `tasktiger/worker.py:812–819`

Task `args` and `kwargs` are written to structured logs:

```python
# executor.py:168
log.info("processing", func=..., task_id=..., params={"args": task.args, "kwargs": task.kwargs})
```

Failed task tracebacks and arguments are also stored in Redis under `t:task:<id>:executions`:

```python
# executor.py:165-166
if self.worker.store_tracebacks:
    execution["traceback"] = "".join(traceback.format_exception(*exc_info))
```

If task arguments contain credentials, tokens, PII, or other secrets, they will appear in logs and persist in Redis. There is no redaction mechanism.

### M7. Queue names are not validated and can contain colons
**File:** `tasktiger/tasktiger.py:296–302`

```python
def _key(self, *parts: str) -> str:
    return ":".join([self.config["REDIS_PREFIX"]] + list(parts))
```

Queue names are joined directly into Redis keys with `:` as a delimiter and no sanitization. A queue named `"foo:active"` would make `_key(QUEUED, "foo:active")` → `"t:queued:foo:active"`, which is identical to the active-queue key for queue `"foo"`. Since queue names can be set by callers of `tiger.delay()`, a malicious or buggy caller can cause key-space collisions.

---

## LOW — Reliability & Performance

### L1. `Semaphore` re-reads and re-registers its Lua script on every instantiation
**File:** `tasktiger/redis_semaphore.py:43–49`

```python
with open(os.path.join(..., "lua/semaphore.lua")) as f:
    self._semaphore = self.redis.register_script(f.read())
```

`Semaphore` is instantiated for every task in `_get_queue_lock()` (`worker.py:411`). Each instantiation performs a disk read and a `SCRIPT LOAD` round-trip to Redis. The script should be registered once (e.g., as a class-level or module-level constant) and reused.

### L2. Lua `unpack()` can hit stack limits with large batch sizes
**Files:** `tasktiger/redis_scripts.py:188`, `redis_scripts.py:223`

```lua
redis.call('zadd', destination, unpack(new_scoremembers))
```

`new_scoremembers` contains `2 × batch_size` elements (alternating score and member). Lua 5.1's `unpack()` is limited by the stack size (typically ~8000 items). With `SCHEDULED_TASK_BATCH_SIZE=1000` (the default), the table will have 2000 elements — safe today, but a configuration change to a large batch size could cause silent Lua errors.

### L3. Non-atomic check in expired task cleanup (acknowledged)
**File:** `tasktiger/worker.py:334–343`

```python
# XXX: Ideally, the following block should be atomic.
if not self.connection.get(self._key("task", task_id)):
    ...
    task._move()
```

The code itself notes this race condition: another worker could re-enqueue the task between the `GET` and `_move()`, causing the just-requeued task to be silently deleted. The low probability is mitigated by the distributed lock, but the window exists.

### L4. Deprecated `datetime.utcnow()` used in multiple places
**Files:** `tasktiger/_internal.py:149`, `tasktiger/task.py:644`

```python
when = datetime.datetime.utcnow() + when   # _internal.py:149
now = datetime.datetime.utcnow()           # task.py:644
```

`datetime.datetime.utcnow()` is deprecated as of Python 3.12 and will be removed in a future version. The replacement is `datetime.datetime.now(datetime.timezone.utc)`. Both usages return naive datetimes, which can cause incorrect comparisons if mixed with timezone-aware datetimes elsewhere.

### L5. Naive vs. aware datetime comparison in `purge_errored_tasks`
**File:** `tasktiger/tasktiger.py:639–643`

```python
if last_execution_before and task.ts and task.ts > last_execution_before:
    continue
```

`task.ts` is constructed via `datetime.datetime.utcfromtimestamp()` (naive). If a caller passes a timezone-aware `last_execution_before`, Python will raise a `TypeError`. No check or normalization is performed.

### L6. `signal.setitimer` cancelled with `signal.alarm(0)` — potential confusion
**File:** `tasktiger/timeouts.py:57`, `tasktiger/timeouts.py:63`

```python
def setup_death_penalty(self):
    signal.signal(signal.SIGALRM, self.handle_death_penalty)
    signal.setitimer(signal.ITIMER_REAL, self._timeout)   # supports sub-second

def cancel_death_penalty(self):
    signal.alarm(0)                                        # integer-only
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
```

The timer is set with `signal.setitimer()` (which accepts float seconds), but cancelled with `signal.alarm(0)` (integer). On Linux and macOS, both functions share the same `ITIMER_REAL` timer, so `alarm(0)` does correctly cancel a timer set by `setitimer`. However, the asymmetry is misleading and non-portable: `alarm(0)` replaces the remaining time with 0, which on some platforms may behave differently from `setitimer(ITIMER_REAL, 0)`. Using `signal.setitimer(signal.ITIMER_REAL, 0)` in `cancel_death_penalty` would be more correct and explicit.

---

## Informational

### I1. Task execution model inherently trusts Redis data
The entire execution model (task function name, args, kwargs, retry strategy, runner class) is controlled by data stored in Redis. If Redis is compromised or shared with untrusted parties, full remote code execution is possible. Redis should be treated as a trusted internal service and protected with authentication (`requirepass`), network ACLs, and TLS.

### I2. Execution history (`executions` list) has no TTL
Task execution history is stored in `t:task:<id>:executions` lists with no expiry. Unless `max_stored_executions` is configured per-task, old error history accumulates indefinitely and can grow the Redis keyspace unboundedly.

### I3. `rollbar.py` parses structlog JSON and silently drops unparseable messages
**File:** `tasktiger/rollbar.py`

The Rollbar handler wraps `json.loads()` in a bare `except` clause, silently swallowing all parse errors. Errors that fail to parse are not reported to Rollbar, creating a blind spot for malformed log records.
