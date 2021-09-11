# stub for SortedSet. This is a lightly edited copy of
# https://github.com/grantjenks/python-sortedcontainers/blob/d0a225d7fd0fb4c54532b8798af3cbeebf97e2d5/sortedcontainers/sortedset.pyi
# (from https://github.com/grantjenks/python-sortedcontainers/pull/107)

from typing import (
    AbstractSet,
    Any,
    Callable,
    Generic,
    Hashable,
    Iterable,
    Iterator,
    List,
    MutableSet,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    overload,
)

# --- Global

_T = TypeVar("_T", bound=Hashable)
_S = TypeVar("_S", bound=Hashable)
_SS = TypeVar("_SS", bound=SortedSet)
_Key = Callable[[_T], Any]

class SortedSet(MutableSet[_T], Sequence[_T]):
    def __init__(
        self,
        iterable: Optional[Iterable[_T]] = ...,
        key: Optional[_Key[_T]] = ...,
    ) -> None: ...
    @classmethod
    def _fromset(
        cls, values: Set[_T], key: Optional[_Key[_T]] = ...
    ) -> SortedSet[_T]: ...
    @property
    def key(self) -> Optional[_Key[_T]]: ...
    def __contains__(self, value: Any) -> bool: ...
    @overload
    def __getitem__(self, index: int) -> _T: ...
    @overload
    def __getitem__(self, index: slice) -> List[_T]: ...
    def __delitem__(self, index: Union[int, slice]) -> None: ...
    def __eq__(self, other: Any) -> bool: ...
    def __ne__(self, other: Any) -> bool: ...
    def __lt__(self, other: Iterable[_T]) -> bool: ...
    def __gt__(self, other: Iterable[_T]) -> bool: ...
    def __le__(self, other: Iterable[_T]) -> bool: ...
    def __ge__(self, other: Iterable[_T]) -> bool: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[_T]: ...
    def __reversed__(self) -> Iterator[_T]: ...
    def add(self, value: _T) -> None: ...
    def _add(self, value: _T) -> None: ...
    def clear(self) -> None: ...
    def copy(self: _SS) -> _SS: ...
    def __copy__(self: _SS) -> _SS: ...
    def count(self, value: _T) -> int: ...
    def discard(self, value: _T) -> None: ...
    def _discard(self, value: _T) -> None: ...
    def pop(self, index: int = ...) -> _T: ...
    def remove(self, value: _T) -> None: ...
    def difference(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __sub__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def difference_update(
        self, *iterables: Iterable[_S]
    ) -> SortedSet[Union[_T, _S]]: ...
    def __isub__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def intersection(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __and__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __rand__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def intersection_update(
        self, *iterables: Iterable[_S]
    ) -> SortedSet[Union[_T, _S]]: ...
    def __iand__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def symmetric_difference(self, other: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __xor__(self, other: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __rxor__(self, other: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def symmetric_difference_update(
        self, other: Iterable[_S]
    ) -> SortedSet[Union[_T, _S]]: ...
    def __ixor__(self, other: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def union(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __or__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __ror__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def update(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __ior__(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def _update(self, *iterables: Iterable[_S]) -> SortedSet[Union[_T, _S]]: ...
    def __reduce__(
        self,
    ) -> Tuple[Type[SortedSet[_T]], Set[_T], Callable[[_T], Any]]: ...
    def __repr__(self) -> str: ...
    def _check(self) -> None: ...
    def bisect_left(self, value: _T) -> int: ...
    def bisect_right(self, value: _T) -> int: ...
    def islice(
        self,
        start: Optional[int] = ...,
        stop: Optional[int] = ...,
        reverse=bool,
    ) -> Iterator[_T]: ...
    def irange(
        self,
        minimum: Optional[_T] = ...,
        maximum: Optional[_T] = ...,
        inclusive: Tuple[bool, bool] = ...,
        reverse: bool = ...,
    ) -> Iterator[_T]: ...
    def index(
        self, value: _T, start: Optional[int] = ..., stop: Optional[int] = ...
    ) -> int: ...
    def _reset(self, load: int) -> None: ...
