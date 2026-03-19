# Security & Bug Audit Report — TaskTiger

**Date:** 2026-03-19
**Scope:** Full source code review of the `tasktiger/` package, `scripts/`, and Lua scripts.

---

## Critical Security Issues

### 1. Arbitrary Code Execution via Deserialized Task Data (Critical)

**Files:** `tasktiger/_internal.py:63-71`, `tasktiger/task.py:274`, `tasktiger/worker.py:762,793`, `tasktiger/runner.py:95`

The `import_attribute` function dynamically imports and returns any Python attribute given a dotted/colon-separated path string:

```python
# _internal.py:63-71
def import_attribute(name: str) -> Any:
    """Return an attribute from a dotted path name (e.g. "path.to.func")."""
    try:
        sep = ":" if ":" in name else "."
        module_name, attribute = name.rsplit(sep, 1)
        module = importlib.import_module(module_name)
        return operator.attrgetter(attribute)(module)
    except (ValueError, ImportError, AttributeError) as e:
        raise TaskImportError(e)
```

This function is called on data deserialized from Redis in multiple places:
- **`task.py:274`** — Resolving the task function to execute from `self._data["func"]`.
- **`worker.py:762`** — Importing exception classes from `execution.get("exception_name")`.
- **`worker.py:793`** — Importing the retry function from `retry_func`.
- **`runner.py:95`** — Importing the runner class from `runner_class_path`.

If an attacker gains write access to the Redis instance (e.g., via network exposure, default credentials, or a Redis exploit), they can inject a malicious `func`, `exception_name`, `retry_method`, or `runner_class` value into a task's JSON data. When a worker processes that task, it will import and execute arbitrary Python code.

**Risk:** Remote code execution if Redis is compromised.

---

### 2. Redis Password Exposed on Command Line (Medium)

**File:** `tasktiger/tasktiger.py:709,735-736`

```python
# tasktiger.py:709
@click.option("-a", "--password", help="Redis password")
```

The Redis password is accepted as a CLI argument. On Linux, command-line arguments are visible to all users on the system via `/proc/<pid>/cmdline` or `ps aux`. This leaks the Redis credential to any local user.

**Risk:** Credential exposure to local users.

---

### 3. Sensitive Data Logged and Stored in Redis (Medium)

**Files:** `tasktiger/worker.py:813-819`, `tasktiger/executor.py:164-166,234-236`

Task arguments and keyword arguments (which may contain passwords, tokens, PII, etc.) are logged in plaintext on task errors and when processing starts:

```python
# worker.py:813-816
log_context.update({
    "task_args": task.args,
    "task_kwargs": task.kwargs,
    ...
})
```

```python
# executor.py:234-236
log.info(
    "processing",
    ...
    params={"args": task.args, "kwargs": task.kwargs},
)
```

Additionally, full Python tracebacks (which may contain variable values and file paths) are stored in Redis:

```python
# executor.py:164-166
if self.worker.store_tracebacks:
    execution["traceback"] = "".join(traceback.format_exception(*exc_info))
```

**Risk:** Sensitive data leakage through logs and Redis storage.

---

### 4. Semaphore Clock Skew Vulnerability (Medium)

**Files:** `tasktiger/redis_semaphore.py:99-102`, `tasktiger/lua/semaphore.lua:22-25`

The semaphore uses client-provided wall-clock time rather than Redis server time:

```python
# redis_semaphore.py:99-102
acquired, locks = self._semaphore(
    keys=[self.name],
    args=[self.lock_id, self.max_locks, self.timeout, time.time()],
)
```

The Lua script itself contains a TODO acknowledging this issue:

```lua
-- semaphore.lua:22-25
-- TODO: Dynamically enable Redis time usage in Redis 3.2+
--local time = redis.call("time")
--local now = tonumber(time[1])
local now = tonumber(ARGV[4])
```

If workers have clock skew, locks can be prematurely expired or held beyond their intended timeout, breaking mutual exclusion guarantees.

**Risk:** Lock safety violations under clock skew; potential duplicate task execution.

---

### 5. Default Redis Connection Without TLS (Low)

**Files:** `tasktiger/tasktiger.py:251,735-736`

```python
# tasktiger.py:251
self.connection: redis.Redis = connection or redis.Redis(decode_responses=True)

# tasktiger.py:735-736
conn = redis.Redis(
    host, int(port or 6379), int(db or 0), password, decode_responses=True
)
```

The default Redis connection and the CLI-created connection do not use TLS. The CLI does not expose an option to enable TLS. All data (including the password if provided) is transmitted in plaintext.

**Risk:** Eavesdropping on Redis traffic (passwords, task data) on untrusted networks.

---

## Bugs

### 6. Unbound/Stale Variable `data` in `tasks_from_queue` (High)

**File:** `tasktiger/task.py:548-568,569-581`

When `serialized_data is None` and `include_not_found is False`, the variable `data` is never assigned in that loop iteration, but the code proceeds to create a `Task` using it:

```python
# task.py:548-568 (with load_executions)
for idx, serialized_data, serialized_executions, ts in zip(
    range(len(items)), results[0], results[1:], tss
):
    if serialized_data is None:
        if include_not_found:
            data = {"id": items[idx][0]}
        # BUG: no else branch — 'data' is never set when include_not_found=False
    else:
        data = json.loads(serialized_data)

    executions = [json.loads(e) for e in serialized_executions if e]

    task = Task(             # <— uses potentially unbound 'data'
        tiger,
        queue=queue,
        _data=data,
        ...
    )
    tasks.append(task)
```

The same pattern occurs in lines 569-581 (without `load_executions`).

- On the first iteration: raises `UnboundLocalError`.
- On subsequent iterations: silently uses `data` from the *previous* iteration, creating a Task with wrong data.

**Impact:** Crash or silent data corruption when tasks are missing from Redis.

---

### 7. Race Condition in `max_queue_size` Check (Medium)

**File:** `tasktiger/task.py:394-399`

```python
# task.py:394-399
if max_queue_size:
    queue_size = tiger.get_total_queue_size(self.queue)
    if queue_size >= max_queue_size:
        raise QueueFullException("Queue size: {}".format(queue_size))
```

The check and the subsequent pipeline-based enqueue (lines 404-417) are not atomic. Between the size check and the ZADD, other processes can add tasks, exceeding the intended maximum. This is a classic TOCTOU (time-of-check-time-of-use) race.

**Impact:** Queue can exceed `max_queue_size` under concurrent writes.

---

### 8. `batch` Parameter Check is Always True (Low)

**File:** `tasktiger/tasktiger.py:361`

```python
# tasktiger.py:361
if batch is not None:
    func._task_batch = batch
```

The parameter `batch` has a default value of `False` (line 319), not `None`. So this condition is always `True`, and `_task_batch` is always set. This is likely a logic error — the intent was probably to only set `_task_batch` when `batch` was explicitly provided (like the other parameters which default to `None`).

**Impact:** Benign because `False` is the default, but misleading and inconsistent with the other parameter patterns.

---

### 9. `assert` Used for Input Validation (Medium)

**Files:** `tasktiger/tasktiger.py:121`, `tasktiger/task.py:89,204,432`, `tasktiger/_internal.py:181-186`, `tasktiger/schedule.py:54,78`

Multiple places use `assert` statements for validating user-supplied input:

```python
# tasktiger.py:121
assert connection is None and config is None and setup_structlog is False

# _internal.py:181-186
assert not isinstance(only_queues, str), error_template.format(...)
assert not isinstance(exclude_queues, str), error_template.format(...)

# schedule.py:54
assert period > 0, "Must specify a positive period."
```

Python's `assert` statements are stripped when running with the `-O` (optimize) flag. This means all these validation checks silently disappear, allowing invalid data to pass through.

**Impact:** Validation bypassed when running in optimized mode (`python -O`).

---

### 10. Deprecated `utcnow()` and `utcfromtimestamp()` Usage (Low)

**Files:** `tasktiger/task.py:184,196,493,536,644`, `tasktiger/_internal.py:149`

```python
# task.py:184
return datetime.datetime.utcfromtimestamp(timestamp)

# task.py:644
now = datetime.datetime.utcnow()

# _internal.py:149
when = datetime.datetime.utcnow() + when
```

These methods return timezone-naive datetime objects and are deprecated since Python 3.12. They can cause subtle bugs when mixed with timezone-aware datetimes (e.g., in `worker.py:968` which uses `datetime.datetime.now(datetime.timezone.utc)`).

**Impact:** Potential timezone-related bugs; deprecation warnings in Python 3.12+.

---

### 11. Unclosed File Descriptors on Exception in `ForkExecutor` (Medium)

**File:** `tasktiger/executor.py:248-378`

The pipe file descriptors and the `opened_fd` object are created at lines 248-250:

```python
# executor.py:248-250
pipe_r, pipe_w = os.pipe()
opened_fd = os.fdopen(pipe_r)
```

The cleanup at lines 375-378 is not in a `finally` block:

```python
# executor.py:375-378
signal.signal(signal.SIGCHLD, signal.SIG_DFL)
signal.set_wakeup_fd(old_wakeup_fd)
opened_fd.close()
os.close(pipe_w)
```

If an unhandled exception occurs in the `while True` loop (lines 301-373), these file descriptors will leak.

**Impact:** File descriptor leak on unexpected exceptions.

---

### 12. Recursive `check_child_exit` on EINTR (Low)

**File:** `tasktiger/executor.py:263-281`

```python
# executor.py:276-278
if e.errno == errno.EINTR:
    return check_child_exit()
```

`check_child_exit` calls itself recursively when it receives `EINTR`. While unlikely to loop many times, repeated `EINTR` signals could exhaust the call stack. An iterative `while` loop would be safer.

**Impact:** Potential stack overflow under pathological signal conditions.

---

### 13. Signal Handler Performs Non-Async-Signal-Safe Operations (Low)

**File:** `tasktiger/worker.py:138-140`

```python
# worker.py:138-140
def request_stop(signum: int, frame: Any) -> None:
    self._stop_requested = True
    self.log.info("stop requested, waiting for task to finish")
```

The `request_stop` signal handler calls `self.log.info()`, which involves I/O, memory allocation, and potentially acquiring locks (structlog/logging internals). These are not async-signal-safe operations and can cause deadlocks if the signal arrives while the main thread holds the logging lock.

**Impact:** Potential deadlock on shutdown signal.

---

### 14. `_queue_for_next_period` Return Type Mismatch (Low)

**File:** `tasktiger/task.py:643-660`

```python
# task.py:660
return when      # when can be None
```

The method signature declares a return type of `float`, but the schedule function can return `None` (e.g., when `end_date` has passed). The caller at `worker.py:906` logs the result without checking for `None`:

```python
# worker.py:906-907
when = task._queue_for_next_period()
self.log.info("queued periodic task", func=task.serialized_func, when=when)
```

**Impact:** Type annotation mismatch; possible `None` propagation where a float is expected.

---

### 15. `_periodic` Ignores Microsecond Component of Timedelta (Low)

**File:** `tasktiger/schedule.py:23`

```python
# schedule.py:23
seconds = delta.seconds + delta.days * 86400
```

The `timedelta.seconds` attribute only returns whole seconds. Microseconds are silently dropped, which means a task could be scheduled slightly before its intended time when `utcnow()` returns a time with a non-zero microsecond component.

**Impact:** Subtle scheduling inaccuracy (up to ~1 second early).

---

### 16. Non-Atomic Iteration in `purge_errored_tasks` (Low)

**File:** `tasktiger/tasktiger.py:626-656`

The `errored_tasks()` generator uses pagination (`skip += task_limit`) over a sorted set that is being mutated (tasks are deleted during iteration at line 650). This means:
- Tasks added between pages could be missed.
- Tasks could shift position, causing some to be skipped or visited twice.

**Impact:** Incomplete or redundant purging of errored tasks.

---

### 17. `RetryException` Inherits from `BaseException` (Low)

**File:** `tasktiger/exceptions.py:32`

```python
class RetryException(BaseException):
```

`RetryException` extends `BaseException` instead of `Exception`. This means user code with bare `except Exception:` blocks will not catch it, potentially causing unexpected worker crashes. While this appears intentional (to prevent tasks from accidentally swallowing the retry), it is a sharp edge for users.

Similarly, `QueueFullException` (line 18) also extends `BaseException`, which means it won't be caught by standard exception handling patterns.

**Impact:** Surprising behavior for users expecting standard exception semantics.

---

### 18. Global Mutable State `g` Not Thread-Safe for `SyncExecutor` (Low)

**File:** `tasktiger/_internal.py:59`

```python
g: _G = {"tiger": None, "current_task_is_batch": None, "current_tasks": None}
```

The global `g` dictionary is mutated during task execution (`executor.py:133-134,141,146,172-174`). While the `ForkExecutor` runs tasks in isolated child processes, the `SyncExecutor` runs tasks in the main process thread. If a task spawns threads that access `TaskTiger.current_task`, they will read from this shared mutable state without synchronization.

**Impact:** Potential race conditions in multi-threaded tasks using `SyncExecutor`.

---

## Summary

| # | Severity | Category | File | Lines | Description |
|---|----------|----------|------|-------|-------------|
| 1 | Critical | Security | `_internal.py` | 63-71 | Arbitrary code execution via `import_attribute` on Redis data |
| 2 | Medium | Security | `tasktiger.py` | 709,735-736 | Redis password exposed on command line |
| 3 | Medium | Security | `worker.py`, `executor.py` | 813-819, 164-166 | Sensitive data in logs and Redis |
| 4 | Medium | Security | `redis_semaphore.py`, `semaphore.lua` | 99-102, 22-25 | Clock skew breaks semaphore safety |
| 5 | Low | Security | `tasktiger.py` | 251,735-736 | Default Redis connection without TLS |
| 6 | High | Bug | `task.py` | 548-568, 569-581 | Unbound/stale variable `data` |
| 7 | Medium | Bug | `task.py` | 394-399 | TOCTOU race in `max_queue_size` check |
| 8 | Low | Bug | `tasktiger.py` | 361 | `batch` check is always True |
| 9 | Medium | Bug | Multiple | Multiple | `assert` used for input validation |
| 10 | Low | Bug | `task.py`, `_internal.py` | Multiple | Deprecated `utcnow`/`utcfromtimestamp` |
| 11 | Medium | Bug | `executor.py` | 248-378 | Unclosed file descriptors on exception |
| 12 | Low | Bug | `executor.py` | 276-278 | Recursive function on EINTR |
| 13 | Low | Bug | `worker.py` | 138-140 | Non-async-signal-safe signal handler |
| 14 | Low | Bug | `task.py` | 643-660 | Return type mismatch in `_queue_for_next_period` |
| 15 | Low | Bug | `schedule.py` | 23 | Microseconds dropped in periodic scheduling |
| 16 | Low | Bug | `tasktiger.py` | 626-656 | Non-atomic iteration in `purge_errored_tasks` |
| 17 | Low | Bug | `exceptions.py` | 18,32 | Exceptions inherit from `BaseException` |
| 18 | Low | Bug | `_internal.py` | 59 | Global `g` not thread-safe for `SyncExecutor` |
