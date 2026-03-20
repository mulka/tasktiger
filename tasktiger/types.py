from typing import Callable

RetryStrategy = tuple[Callable[..., float], tuple]
