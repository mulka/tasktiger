# Deprecated Python Usage in tasktiger

Scan performed on 2026-03-20 against the `audit/deprecated-usage` branch.
Python version support declared in `setup.py`: 3.10, 3.11, 3.12, 3.13.

---

## Critical — datetime timezone-naive APIs (deprecated Python 3.12)

`datetime.datetime.utcnow()` and `datetime.datetime.utcfromtimestamp()` are
deprecated as of Python 3.12 ([bpo-103469]).  They return naïve datetimes that
are ambiguous (no tzinfo), making it impossible for callers to know they
represent UTC.  The deprecation warning is emitted at call-time in 3.12.

**Replacement:** Use `datetime.datetime.now(datetime.timezone.utc)` (works on
all supported versions) or `datetime.datetime.now(datetime.UTC)` (3.11+).

| File | Line | Deprecated call | Replacement |
|------|------|-----------------|-------------|
| `tasktiger/_internal.py` | 149 | `datetime.datetime.utcnow()` | `datetime.datetime.now(datetime.timezone.utc)` |
| `tasktiger/task.py` | 184 | `datetime.datetime.utcfromtimestamp(timestamp)` | `datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)` |
| `tasktiger/task.py` | 196 | `datetime.datetime.utcfromtimestamp(timestamp)` | `datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)` |
| `tasktiger/task.py` | 493 | `datetime.datetime.utcfromtimestamp(score)` | `datetime.datetime.fromtimestamp(score, tz=datetime.timezone.utc)` |
| `tasktiger/task.py` | 536 | `datetime.datetime.utcfromtimestamp(item[1])` | `datetime.datetime.fromtimestamp(item[1], tz=datetime.timezone.utc)` |
| `tasktiger/task.py` | 644 | `datetime.datetime.utcnow()` | `datetime.datetime.now(datetime.timezone.utc)` |
| `tasktiger/tasktiger.py` | 588 | `datetime.datetime.utcnow()` (docstring example) | `datetime.datetime.now(datetime.timezone.utc)` |
| `tests/utils.py` | 91 | `datetime.datetime.utcnow()` | `datetime.datetime.now(datetime.timezone.utc)` |

### Related: `datetime.utctimetuple()` (implicitly affected)

| File | Line | Issue | Replacement |
|------|------|-------|-------------|
| `tasktiger/_internal.py` | 154 | `when.utctimetuple()` — this drops sub-second precision (comment acknowledges this) and only works correctly when `when` is already a naïve UTC datetime. After replacing `utcnow()` with an aware datetime, this call will still work but the fix should be paired with a review to ensure `when` is always UTC-aware. | `calendar.timegm(when.utctimetuple())` can stay once `when` is aware-UTC; alternatively use `when.timestamp()` directly. |

---

## Moderate — `typing` generic aliases (deprecated Python 3.9)

PEP 585 (Python 3.9) made `list`, `dict`, `tuple`, `set`, `frozenset`, and
other built-in and `collections.abc` types directly subscriptable as generics.
The capitalised aliases in `typing` (`List`, `Dict`, `Tuple`, `Set`, etc.) are
**deprecated as of Python 3.9** and will be removed in a future version.

Since `setup.py` declares support for 3.10+, all of these can be replaced with
their built-in equivalents right now with no compatibility shims.

**Replacement summary:**

| `typing` alias | Replacement |
|----------------|-------------|
| `typing.List[X]` | `list[X]` |
| `typing.Dict[K, V]` | `dict[K, V]` |
| `typing.Tuple[X, ...]` | `tuple[X, ...]` |
| `typing.Set[X]` | `set[X]` |
| `typing.FrozenSet[X]` | `frozenset[X]` |
| `typing.Type[X]` | `type[X]` |
| `typing.Optional[X]` | `X \| None` |
| `typing.Union[X, Y]` | `X \| Y` |
| `typing.Callable[..., R]` | `collections.abc.Callable[..., R]` |

**Total occurrences across the package: ~190** (grep for `List\[`, `Dict\[`,
`Tuple\[`, `Set\[`, `Optional\[`, `Union\[`, `Type\[` in `tasktiger/*.py`).

Files affected (occurrence count from grep):

| File | Occurrences |
|------|-------------|
| `tasktiger/tasktiger.py` | 65 |
| `tasktiger/task.py` | 41 |
| `tasktiger/worker.py` | 24 |
| `tasktiger/redis_scripts.py` | 15 |
| `tasktiger/executor.py` | 12 |
| `tasktiger/schedule.py` | 9 |
| `tasktiger/runner.py` | 4 |
| `tasktiger/timeouts.py` | 3 |
| `tasktiger/redis_semaphore.py` | 3 |
| `tasktiger/_internal.py` | 9 |
| `tasktiger/logging.py` | 2 |
| `tasktiger/flask_script.py` | 1 |
| `tasktiger/stats.py` | 1 |
| `tasktiger/types.py` | 1 |

Note: `typing.Callable` and `typing.Tuple` (used as bare imports in
`tasktiger/types.py` line 1) should migrate to `collections.abc.Callable` and
the built-in `tuple`.

---

## No issues found

The following deprecated patterns were checked and are **not present** in this
codebase:

- `collections.Callable` / `collections.Mapping` etc. (deprecated 3.3, removed 3.10)
- `@asyncio.coroutine` / `yield from` coroutine style (deprecated 3.8, removed 3.11)
- `asyncio.get_event_loop()` misuse without a running loop (deprecated 3.10)
- `loop=` parameter in asyncio APIs (deprecated 3.8, removed 3.10)
- `threading.currentThread()` / `threading.activeCount()` aliases
- `inspect.getargspec()` (deprecated 3.0, removed 3.11)
- `inspect.getcallargs()` (deprecated 3.5)
- `imp` module (deprecated 3.4, removed 3.12)
- `distutils` (deprecated 3.10, removed 3.12) — `setup.py` uses `setuptools`
- `cgi` / `pipes` / `telnetlib` modules (deprecated 3.11)
- `typing.io` / `typing.re` (removed 3.12)
- Deprecated `unittest` assertion aliases (`assertEquals`, `assertRaisesRegexp`, etc.)
- `pkg_resources` (deprecated in favor of `importlib.resources`)
- Python 2 `print` statements

---

## Recommended fix order

1. **Fix `utcnow()` / `utcfromtimestamp()` calls first** — these are the only
   changes that produce runtime `DeprecationWarning` on Python 3.12 today and
   will eventually become errors.
2. **Migrate `typing` generics** — purely cosmetic on 3.10+ but removes
   deprecation noise and future-proofs the codebase.  A codemod tool such as
   [`pyupgrade --py310-plus`](https://github.com/asottile/pyupgrade) can
   automate most of this in a single pass.

[bpo-103469]: https://github.com/python/cpython/issues/103469
