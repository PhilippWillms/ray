import functools
import importlib
import logging
import os
import pathlib
import platform
import random
import sys
import threading
import time
import urllib.parse
from collections import Counter
from queue import Empty, Full, Queue
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

import numpy as np
import pandas as pd
import pyarrow
from packaging.version import parse as parse_version

import ray
from ray._private.arrow_utils import get_pyarrow_version
from ray.data.context import DEFAULT_READ_OP_MIN_NUM_BLOCKS, WARN_PREFIX, DataContext

import psutil

if TYPE_CHECKING:
    import pandas

    from ray.data._internal.compute import ComputeStrategy
    from ray.data._internal.execution.interfaces import RefBundle
    from ray.data._internal.planner.exchange.sort_task_spec import SortKey
    from ray.data.block import (
        Block,
        BlockMetadataWithSchema,
        Schema,
        UserDefinedFunction,
    )
    from ray.data.datasource import Datasource, Reader
    from ray.util.placement_group import PlacementGroup

logger = logging.getLogger(__name__)


KiB = 1024  # bytes
MiB = 1024 * KiB
GiB = 1024 * MiB


SENTINEL = object()


# NOTE: Make sure that these lower and upper bounds stay in sync with version
# constraints given in python/setup.py.
# Inclusive minimum pyarrow version.
MIN_PYARROW_VERSION = "6.0.1"
RAY_DISABLE_PYARROW_VERSION_CHECK = "RAY_DISABLE_PYARROW_VERSION_CHECK"
_VERSION_VALIDATED = False
_LOCAL_SCHEME = "local"
_EXAMPLE_SCHEME = "example"


LazyModule = Union[None, bool, ModuleType]
_pyarrow_dataset: LazyModule = None


class _OrderedNullSentinel:
    """Sentinel value that sorts greater than any other non-null value.

    NOTE: Semantic of this sentinel is closely mirroring that one of
          ``np.nan`` for the purpose of consistency in handling of
          ``None``s and ``np.nan``s.
    """

    def __eq__(self, other):
        return False

    def __lt__(self, other):
        # not None < _OrderedNullSentinel
        # _OrderedNullSentinel < _OrderedNullSentinel
        # _OrderedNullSentinel < None
        # _OrderedNullSentinel < np.nan
        return isinstance(other, _OrderedNullSentinel) or is_null(other)

    def __le__(self, other):
        # NOTE: This is just a shortened version of
        #   self < other or self == other
        return self.__lt__(other)

    def __gt__(self, other):
        return not self.__le__(other)

    def __ge__(self, other):
        return not self.__lt__(other)

    def __hash__(self):
        return id(self)


NULL_SENTINEL = _OrderedNullSentinel()


def _lazy_import_pyarrow_dataset() -> LazyModule:
    global _pyarrow_dataset
    if _pyarrow_dataset is None:
        try:
            from pyarrow import dataset as _pyarrow_dataset
        except ModuleNotFoundError:
            # If module is not found, set _pyarrow to False so we won't
            # keep trying to import it on every _lazy_import_pyarrow() call.
            _pyarrow_dataset = False
    return _pyarrow_dataset


def _check_pyarrow_version():
    """Check that pyarrow's version is within the supported bounds."""
    global _VERSION_VALIDATED

    if not _VERSION_VALIDATED:
        if os.environ.get(RAY_DISABLE_PYARROW_VERSION_CHECK, "0") == "1":
            _VERSION_VALIDATED = True
            return

        version = get_pyarrow_version()
        if version is not None:
            if version < parse_version(MIN_PYARROW_VERSION):
                raise ImportError(
                    f"Dataset requires pyarrow >= {MIN_PYARROW_VERSION}, but "
                    f"{version} is installed. Reinstall with "
                    f'`pip install -U "pyarrow"`. '
                    "If you want to disable this pyarrow version check, set the "
                    f"environment variable {RAY_DISABLE_PYARROW_VERSION_CHECK}=1."
                )
        else:
            logger.warning(
                "You are using the 'pyarrow' module, but the exact version is unknown "
                "(possibly carried as an internal component by another module). Please "
                f"make sure you are using pyarrow >= {MIN_PYARROW_VERSION} to ensure "
                "compatibility with Ray Dataset. "
                "If you want to disable this pyarrow version check, set the "
                f"environment variable {RAY_DISABLE_PYARROW_VERSION_CHECK}=1."
            )
        _VERSION_VALIDATED = True


def _autodetect_parallelism(
    parallelism: int,
    target_max_block_size: Optional[int],
    ctx: DataContext,
    datasource_or_legacy_reader: Optional[Union["Datasource", "Reader"]] = None,
    mem_size: Optional[int] = None,
    placement_group: Optional["PlacementGroup"] = None,
    avail_cpus: Optional[int] = None,
) -> Tuple[int, str, Optional[int]]:
    """Returns parallelism to use and the min safe parallelism to avoid OOMs.

    This detects parallelism using the following heuristics, applied in order:

     1) We start with the default value of 200. This can be overridden by
        setting the `read_op_min_num_blocks` attribute of
        :class:`~ray.data.context.DataContext`.
     2) Min block size. If the parallelism would make blocks smaller than this
        threshold, the parallelism is reduced to avoid the overhead of tiny blocks.
     3) Max block size. If the parallelism would make blocks larger than this
        threshold, the parallelism is increased to avoid OOMs during processing.
     4) Available CPUs. If the parallelism cannot make use of all the available
        CPUs in the cluster, the parallelism is increased until it can.

    Args:
        parallelism: The user-requested parallelism, or -1 for auto-detection.
        target_max_block_size: The target max block size to
            produce. We pass this separately from the
            DatasetContext because it may be set per-op instead of
            per-Dataset.
        ctx: The current Dataset context to use for configs.
        datasource_or_legacy_reader: The datasource or legacy reader, to be used for
            data size estimation.
        mem_size: If passed, then used to compute the parallelism according to
            target_max_block_size.
        placement_group: The placement group that this Dataset
            will execute inside, if any.
        avail_cpus: Override avail cpus detection (for testing only).

    Returns:
        Tuple of detected parallelism (only if -1 was specified), the reason
        for the detected parallelism (only if -1 was specified), and the estimated
        inmemory size of the dataset.
    """
    min_safe_parallelism = 1
    max_reasonable_parallelism = sys.maxsize

    if mem_size is None and datasource_or_legacy_reader:
        mem_size = datasource_or_legacy_reader.estimate_inmemory_data_size()
    if (
        mem_size is not None
        and not np.isnan(mem_size)
        and target_max_block_size is not None
    ):
        min_safe_parallelism = max(1, int(mem_size / target_max_block_size))
        max_reasonable_parallelism = max(1, int(mem_size / ctx.target_min_block_size))

    reason = ""
    if parallelism < 0:
        if parallelism != -1:
            raise ValueError("`parallelism` must either be -1 or a positive integer.")

        if (
            ctx.min_parallelism is not None
            and ctx.min_parallelism != DEFAULT_READ_OP_MIN_NUM_BLOCKS
            and ctx.read_op_min_num_blocks == DEFAULT_READ_OP_MIN_NUM_BLOCKS
        ):
            logger.warning(
                "``DataContext.min_parallelism`` is deprecated in Ray 2.10. "
                "Please specify ``DataContext.read_op_min_num_blocks`` instead."
            )
            ctx.read_op_min_num_blocks = ctx.min_parallelism

        # Start with 2x the number of cores as a baseline, with a min floor.
        if placement_group is None:
            placement_group = ray.util.get_current_placement_group()
        avail_cpus = avail_cpus or _estimate_avail_cpus(placement_group)
        parallelism = max(
            min(ctx.read_op_min_num_blocks, max_reasonable_parallelism),
            min_safe_parallelism,
            avail_cpus * 2,
        )

        if parallelism == ctx.read_op_min_num_blocks:
            reason = (
                "DataContext.get_current().read_op_min_num_blocks="
                f"{ctx.read_op_min_num_blocks}"
            )
        elif parallelism == max_reasonable_parallelism:
            reason = (
                "output blocks of size at least "
                "DataContext.get_current().target_min_block_size="
                f"{ctx.target_min_block_size / MiB} MiB"
            )
        elif parallelism == min_safe_parallelism:
            # Handle ``None`` (unlimited) gracefully in the log message.
            if ctx.target_max_block_size is None:
                display_val = "unlimited"
            else:
                display_val = f"{ctx.target_max_block_size / MiB} MiB"
            reason = (
                "output blocks of size at most "
                "DataContext.get_current().target_max_block_size="
                f"{display_val}"
            )
        else:
            reason = (
                "parallelism at least twice the available number "
                f"of CPUs ({avail_cpus})"
            )

        logger.debug(
            f"Autodetected parallelism={parallelism} based on "
            f"estimated_available_cpus={avail_cpus} and "
            f"estimated_data_size={mem_size}."
        )

    return parallelism, reason, mem_size


def _estimate_avail_cpus(cur_pg: Optional["PlacementGroup"]) -> int:
    """Estimates the available CPU parallelism for this Dataset in the cluster.

    If we aren't in a placement group, this is trivially the number of CPUs in the
    cluster. Otherwise, we try to calculate how large the placement group is relative
    to the size of the cluster.

    Args:
        cur_pg: The current placement group, if any.
    """
    cluster_cpus = int(ray.cluster_resources().get("CPU", 1))
    cluster_gpus = int(ray.cluster_resources().get("GPU", 0))

    # If we're in a placement group, we shouldn't assume the entire cluster's
    # resources are available for us to use. Estimate an upper bound on what's
    # reasonable to assume is available for datasets to use.
    if cur_pg:
        pg_cpus = 0
        for bundle in cur_pg.bundle_specs:
            # Calculate the proportion of the cluster this placement group "takes up".
            # Then scale our cluster_cpus proportionally to avoid over-parallelizing
            # if there are many parallel Tune trials using the cluster.
            cpu_fraction = bundle.get("CPU", 0) / max(1, cluster_cpus)
            gpu_fraction = bundle.get("GPU", 0) / max(1, cluster_gpus)
            max_fraction = max(cpu_fraction, gpu_fraction)
            # Over-parallelize by up to a factor of 2, but no more than that. It's
            # preferrable to over-estimate than under-estimate.
            pg_cpus += 2 * int(max_fraction * cluster_cpus)

        return min(cluster_cpus, pg_cpus)

    return cluster_cpus


def _estimate_available_parallelism() -> int:
    """Estimates the available CPU parallelism for this Dataset in the cluster.
    If we are currently in a placement group, take that into account."""
    cur_pg = ray.util.get_current_placement_group()
    return _estimate_avail_cpus(cur_pg)


def _warn_on_high_parallelism(requested_parallelism, num_read_tasks):
    available_cpu_slots = ray.available_resources().get("CPU", 1)
    if (
        requested_parallelism
        and num_read_tasks > available_cpu_slots * 4
        and num_read_tasks >= 5000
    ):
        logger.warning(
            f"{WARN_PREFIX} The requested parallelism of {requested_parallelism} "
            "is more than 4x the number of available CPU slots in the cluster of "
            f"{available_cpu_slots}. This can "
            "lead to slowdowns during the data reading phase due to excessive "
            "task creation. Reduce the parallelism to match with the available "
            "CPU slots in the cluster, or set parallelism to -1 for Ray Data "
            "to automatically determine the parallelism. "
            "You can ignore this message if the cluster is expected to autoscale."
        )


def _check_import(obj, *, module: str, package: str) -> None:
    """Check if a required dependency is installed.

    If `module` can't be imported, this function raises an `ImportError` instructing
    the user to install `package` from PyPI.

    Args:
        obj: The object that has a dependency.
        module: The name of the module to import.
        package: The name of the package on PyPI.
    """
    try:
        importlib.import_module(module)
    except ImportError:
        raise ImportError(
            f"`{obj.__class__.__name__}` depends on '{module}', but Ray Data couldn't "
            f"import it. Install '{module}' by running `pip install {package}`."
        )


def _resolve_custom_scheme(path: str) -> str:
    """Returns the resolved path if the given path follows a Ray-specific custom
    scheme. Othewise, returns the path unchanged.

    The supported custom schemes are: "local", "example".
    """
    parsed_uri = urllib.parse.urlparse(path)
    if parsed_uri.scheme == _LOCAL_SCHEME:
        path = parsed_uri.netloc + parsed_uri.path
    elif parsed_uri.scheme == _EXAMPLE_SCHEME:
        example_data_path = pathlib.Path(__file__).parent.parent / "examples" / "data"
        path = example_data_path / (parsed_uri.netloc + parsed_uri.path)
        path = str(path.resolve())
    return path


def _is_local_scheme(paths: Union[str, List[str]]) -> bool:
    """Returns True if the given paths are in local scheme.
    Note: The paths must be in same scheme, i.e. it's invalid and
    will raise error if paths are mixed with different schemes.
    """
    if isinstance(paths, str):
        paths = [paths]
    if isinstance(paths, pathlib.Path):
        paths = [str(paths)]
    elif not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError("paths must be a path string or a list of path strings.")
    elif len(paths) == 0:
        raise ValueError("Must provide at least one path.")
    num = sum(urllib.parse.urlparse(path).scheme == _LOCAL_SCHEME for path in paths)
    if num > 0 and num < len(paths):
        raise ValueError(
            "The paths must all be local-scheme or not local-scheme, "
            f"but found mixed {paths}"
        )
    return num == len(paths)


def _truncated_repr(obj: Any) -> str:
    """Utility to return a truncated object representation for error messages."""
    msg = str(obj)
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg


def _insert_doc_at_pattern(
    obj,
    *,
    message: str,
    pattern: str,
    insert_after: bool = True,
    directive: Optional[str] = None,
    skip_matches: int = 0,
) -> str:
    if "\n" in message:
        raise ValueError(
            "message shouldn't contain any newlines, since this function will insert "
            f"its own linebreaks when text wrapping: {message}"
        )

    doc = obj.__doc__.strip()
    if not doc:
        doc = ""

    if pattern == "" and insert_after:
        # Empty pattern + insert_after means that we want to append the message to the
        # end of the docstring.
        head = doc
        tail = ""
    else:
        tail = doc
        i = tail.find(pattern)
        skip_matches_left = skip_matches
        while i != -1:
            if insert_after:
                # Set offset to the first character after the pattern.
                offset = i + len(pattern)
            else:
                # Set offset to the first character in the matched line.
                offset = tail[:i].rfind("\n") + 1
            head = tail[:offset]
            tail = tail[offset:]
            skip_matches_left -= 1
            if skip_matches_left <= 0:
                break
            elif not insert_after:
                # Move past the found pattern, since we're skipping it.
                tail = tail[i - offset + len(pattern) :]
            i = tail.find(pattern)
        else:
            raise ValueError(
                f"Pattern {pattern} not found after {skip_matches} skips in docstring "
                f"{doc}"
            )
    # Get indentation of the to-be-inserted text.
    after_lines = list(filter(bool, tail.splitlines()))
    if len(after_lines) > 0:
        lines = after_lines
    else:
        lines = list(filter(bool, reversed(head.splitlines())))
    # Should always have at least one non-empty line in the docstring.
    assert len(lines) > 0
    indent = " " * (len(lines[0]) - len(lines[0].lstrip()))
    # Handle directive.
    message = message.strip("\n")
    if directive is not None:
        base = f"{indent}.. {directive}::\n"
        message = message.replace("\n", "\n" + indent + " " * 4)
        message = base + indent + " " * 4 + message
    else:
        message = indent + message.replace("\n", "\n" + indent)
    # Add two blank lines before/after message, if necessary.
    if insert_after ^ (pattern == "\n\n"):
        # Only two blank lines before message if:
        # 1. Inserting message after pattern and pattern is not two blank lines.
        # 2. Inserting message before pattern and pattern is two blank lines.
        message = "\n\n" + message
    if (not insert_after) ^ (pattern == "\n\n"):
        # Only two blank lines after message if:
        # 1. Inserting message before pattern and pattern is not two blank lines.
        # 2. Inserting message after pattern and pattern is two blank lines.
        message = message + "\n\n"

    # Insert message before/after pattern.
    parts = [head, message, tail]
    # Build new docstring.
    obj.__doc__ = "".join(parts)


def _consumption_api(
    if_more_than_read: bool = False,
    datasource_metadata: Optional[str] = None,
    extra_condition: Optional[str] = None,
    delegate: Optional[str] = None,
    pattern="Examples:",
    insert_after=False,
):
    """Annotate the function with an indication that it's a consumption API, and that it
    will trigger Dataset execution.
    """
    base = (
        " will trigger execution of the lazy transformations performed on "
        "this dataset."
    )
    if delegate:
        message = delegate + base
    elif not if_more_than_read:
        message = "This operation" + base
    else:
        condition = "If this dataset consists of more than a read, "
        if datasource_metadata is not None:
            condition += (
                f"or if the {datasource_metadata} can't be determined from the "
                "metadata provided by the datasource, "
            )
        if extra_condition is not None:
            condition += extra_condition + ", "
        message = condition + "then this operation" + base

    def wrap(obj):
        _insert_doc_at_pattern(
            obj,
            message=message,
            pattern=pattern,
            insert_after=insert_after,
            directive="note",
        )
        return obj

    return wrap


def ConsumptionAPI(*args, **kwargs):
    """Annotate the function with an indication that it's a consumption API, and that it
    will trigger Dataset execution.
    """
    if len(args) == 1 and len(kwargs) == 0 and callable(args[0]):
        return _consumption_api()(args[0])
    return _consumption_api(*args, **kwargs)


def _all_to_all_api(*args, **kwargs):
    """Annotate the function with an indication that it's a all to all API, and that it
    is an operation that requires all inputs to be materialized in-memory to execute.
    """

    def wrap(obj):
        _insert_doc_at_pattern(
            obj,
            message=(
                "This operation requires all inputs to be "
                "materialized in object store for it to execute."
            ),
            pattern="Examples:",
            insert_after=False,
            directive="note",
        )
        return obj

    return wrap


def AllToAllAPI(*args, **kwargs):
    """Annotate the function with an indication that it's a all to all API, and that it
    is an operation that requires all inputs to be materialized in-memory to execute.
    """
    # This should only be used as a decorator for dataset methods.
    assert len(args) == 1 and len(kwargs) == 0 and callable(args[0])
    return _all_to_all_api()(args[0])


def get_compute_strategy(
    fn: "UserDefinedFunction",
    fn_constructor_args: Optional[Iterable[Any]] = None,
    compute: Optional[Union[str, "ComputeStrategy"]] = None,
    concurrency: Optional[Union[int, Tuple[int, int]]] = None,
) -> "ComputeStrategy":
    """Get `ComputeStrategy` based on the function or class, and concurrency
    information.

    Args:
        fn: The function or generator to apply to a record batch, or a class type
            that can be instantiated to create such a callable.
        fn_constructor_args: Positional arguments to pass to ``fn``'s constructor.
        compute: Either "tasks" (default) to use Ray Tasks or an
                :class:`~ray.data.ActorPoolStrategy` to use an autoscaling actor pool.
        concurrency: The number of Ray workers to use concurrently.

    Returns:
       The `ComputeStrategy` for execution.
    """
    # Lazily import these objects to avoid circular imports.
    from ray.data._internal.compute import ActorPoolStrategy, TaskPoolStrategy
    from ray.data.block import CallableClass

    if isinstance(fn, CallableClass):
        is_callable_class = True
    else:
        # TODO(chengsu): disallow object that is not a function. For example,
        # An object instance of class often indicates a bug in user code.
        is_callable_class = False
        if fn_constructor_args is not None:
            raise ValueError(
                "``fn_constructor_args`` can only be specified if providing a "
                f"callable class instance for ``fn``, but got: {fn}."
            )

    if compute is not None:
        # Legacy code path to support `compute` argument.
        logger.warning(
            "The argument ``compute`` is deprecated in Ray 2.9. Please specify "
            "argument ``concurrency`` instead. For more information, see "
            "https://docs.ray.io/en/master/data/transforming-data.html#"
            "stateful-transforms."
        )
        if is_callable_class and (
            compute == "tasks" or isinstance(compute, TaskPoolStrategy)
        ):
            raise ValueError(
                "``compute`` must specify an actor compute strategy when using a "
                f"callable class, but got: {compute}. For example, use "
                "``compute=ray.data.ActorPoolStrategy(size=n)``."
            )
        elif not is_callable_class and (
            compute == "actors" or isinstance(compute, ActorPoolStrategy)
        ):
            raise ValueError(
                f"``compute`` is specified as the actor compute strategy: {compute}, "
                f"but ``fn`` is not a callable class: {fn}. Pass a callable class or "
                "use the default ``compute`` strategy."
            )
        return compute
    elif concurrency is not None:
        if isinstance(concurrency, tuple):
            if (
                len(concurrency) == 2
                and isinstance(concurrency[0], int)
                and isinstance(concurrency[1], int)
            ):
                if is_callable_class:
                    return ActorPoolStrategy(
                        min_size=concurrency[0], max_size=concurrency[1]
                    )
                else:
                    raise ValueError(
                        "``concurrency`` is set as a tuple of integers, but ``fn`` "
                        f"is not a callable class: {fn}. Use ``concurrency=n`` to "
                        "control maximum number of workers to use."
                    )
            else:
                raise ValueError(
                    "``concurrency`` is expected to be set as a tuple of "
                    f"integers, but got: {concurrency}."
                )
        elif isinstance(concurrency, int):
            if is_callable_class:
                return ActorPoolStrategy(size=concurrency)
            else:
                return TaskPoolStrategy(size=concurrency)
        else:
            raise ValueError(
                "``concurrency`` is expected to be set as an integer or a "
                f"tuple of integers, but got: {concurrency}."
            )
    else:
        if is_callable_class:
            raise ValueError(
                "``concurrency`` must be specified when using a callable class. "
                "For example, use ``concurrency=n`` for a pool of ``n`` workers."
            )
        else:
            return TaskPoolStrategy()


def capfirst(s: str):
    """Capitalize the first letter of a string

    Args:
        s: String to capitalize

    Returns:
       Capitalized string
    """
    return s[0].upper() + s[1:]


def capitalize(s: str):
    """Capitalize a string, removing '_' and keeping camelcase.

    Args:
        s: String to capitalize

    Returns:
        Capitalized string with no underscores.
    """
    return "".join(capfirst(x) for x in s.split("_"))


def pandas_df_to_arrow_block(
    df: "pandas.DataFrame",
) -> Tuple["Block", "BlockMetadataWithSchema"]:
    from ray.data.block import BlockAccessor, BlockExecStats, BlockMetadataWithSchema

    block = BlockAccessor.for_block(df).to_arrow()
    stats = BlockExecStats.builder()
    return block, BlockMetadataWithSchema.from_block(block, stats=stats.build())


def ndarray_to_block(
    ndarray: np.ndarray, ctx: DataContext
) -> Tuple["Block", "BlockMetadataWithSchema"]:
    from ray.data.block import BlockAccessor, BlockExecStats, BlockMetadataWithSchema

    DataContext._set_current(ctx)

    stats = BlockExecStats.builder()
    block = BlockAccessor.batch_to_block({"data": ndarray})
    return block, BlockMetadataWithSchema.from_block(block, stats=stats.build())


def get_table_block_metadata_schema(
    table: Union["pyarrow.Table", "pandas.DataFrame"],
) -> "BlockMetadataWithSchema":
    from ray.data.block import BlockExecStats, BlockMetadataWithSchema

    stats = BlockExecStats.builder()
    return BlockMetadataWithSchema.from_block(table, stats=stats.build())


def unify_block_metadata_schema(
    block_metadata_with_schemas: List["BlockMetadataWithSchema"],
) -> Optional["Schema"]:
    """For the input list of BlockMetadata, return a unified schema of the
    corresponding blocks. If the metadata have no valid schema, returns None.

    Args:
        block_metadata_with_schemas: List of BlockMetadata to unify

    Returns:
        A unified schema of the input list of schemas, or None if no valid schemas
        are provided.
    """
    # Some blocks could be empty, in which case we cannot get their schema.
    # TODO(ekl) validate schema is the same across different blocks.

    # First check if there are blocks with computed schemas, then unify
    # valid schemas from all such blocks.

    schemas_to_unify = []
    for m in block_metadata_with_schemas:
        if m.schema is not None and (m.num_rows is None or m.num_rows > 0):
            schemas_to_unify.append(m.schema)
    return unify_schemas_with_validation(schemas_to_unify)


def unify_schemas_with_validation(
    schemas_to_unify: Iterable["Schema"],
) -> Optional["Schema"]:
    if schemas_to_unify:
        from ray.data._internal.arrow_ops.transform_pyarrow import unify_schemas

        # Check valid pyarrow installation before attempting schema unification
        try:
            import pyarrow as pa
        except ImportError:
            pa = None
        # If the result contains PyArrow schemas, unify them
        if pa is not None and all(isinstance(s, pa.Schema) for s in schemas_to_unify):
            return unify_schemas(schemas_to_unify, promote_types=True)
        # Otherwise, if the resulting schemas are simple types (e.g. int),
        # return the first schema.
        return schemas_to_unify[0]
    return None


def unify_ref_bundles_schema(
    ref_bundles: List["RefBundle"],
) -> Optional["Schema"]:
    schemas_to_unify = []
    for bundle in ref_bundles:
        if bundle.schema is not None and (
            bundle.num_rows() is None or bundle.num_rows() > 0
        ):
            schemas_to_unify.append(bundle.schema)
    return unify_schemas_with_validation(schemas_to_unify)


def find_partition_index(
    table: Union["pyarrow.Table", "pandas.DataFrame"],
    desired: Tuple[Union[int, float]],
    sort_key: "SortKey",
) -> int:
    """For the given block, find the index where the desired value should be
    added, to maintain sorted order.

    We do this by iterating over each column, starting with the primary sort key,
    and binary searching for the desired value in the column. Each binary search
    shortens the "range" of indices (represented by ``left`` and ``right``, which
    are indices of rows) where the desired value could be inserted.

    Args:
        table: The block to search in.
        desired: A single tuple representing the boundary to partition at.
            ``len(desired)`` must be less than or equal to the number of columns
            being sorted.
        sort_key: The sort key to use for sorting, providing the columns to be
            sorted and their directions.

    Returns:
        The index where the desired value should be inserted to maintain sorted
        order.
    """
    columns = sort_key.get_columns()
    descending = sort_key.get_descending()

    left, right = 0, len(table)
    for i in range(len(desired)):
        if left == right:
            return right
        col_name = columns[i]
        col_vals = table[col_name].to_numpy()[left:right]
        desired_val = desired[i]

        # Handle null values - replace them with sentinel values
        if desired_val is None:
            desired_val = NULL_SENTINEL

        prevleft = left
        if descending[i] is True:
            # ``np.searchsorted`` expects the array to be sorted in ascending
            # order, so we pass ``sorter``, which is an array of integer indices
            # that sort ``col_vals`` into ascending order. The returned index
            # is an index into the ascending order of ``col_vals``, so we need
            # to subtract it from ``len(col_vals)`` to get the index in the
            # original descending order of ``col_vals``.
            sorter = np.arange(len(col_vals) - 1, -1, -1)
            left = prevleft + (
                len(col_vals)
                - np.searchsorted(
                    col_vals,
                    desired_val,
                    side="right",
                    sorter=sorter,
                )
            )
            right = prevleft + (
                len(col_vals)
                - np.searchsorted(
                    col_vals,
                    desired_val,
                    side="left",
                    sorter=sorter,
                )
            )
        else:
            left = prevleft + np.searchsorted(col_vals, desired_val, side="left")
            right = prevleft + np.searchsorted(col_vals, desired_val, side="right")

    return right if descending[0] is True else left


def get_attribute_from_class_name(class_name: str) -> Any:
    """Get Python attribute from the provided class name.

    The caller needs to make sure the provided class name includes
    full module name, and can be imported successfully.
    """
    from importlib import import_module

    paths = class_name.split(".")
    if len(paths) < 2:
        raise ValueError(f"Cannot create object from {class_name}.")

    module_name = ".".join(paths[:-1])
    attribute_name = paths[-1]
    return getattr(import_module(module_name), attribute_name)


T = TypeVar("T")
U = TypeVar("U")


class _InterruptibleQueue(Queue):
    """Extension of Python's `queue.Queue` providing ability to get interrupt its
    method callers in other threads"""

    INTERRUPTION_CHECK_FREQUENCY_SEC = 0.5

    def __init__(
        self, max_size: int, interrupted_event: Optional[threading.Event] = None
    ):
        super().__init__(maxsize=max_size)
        self._interrupted_event = interrupted_event or threading.Event()

    def get(self, block=True, timeout=None):
        if not block or timeout is not None:
            return super().get(block, timeout)

        # In case when the call is blocking and no timeout is specified (ie blocking
        # indefinitely) we apply the following protocol to make it interruptible:
        #
        #   1. `Queue.get` is invoked w/ 500ms timeout
        #   2. `Empty` exception is intercepted (will be raised upon timeout elapsing)
        #   3. If interrupted flag is set `InterruptedError` is raised
        #   4. Otherwise, protocol retried (until interrupted or queue
        #      becoming non-empty)
        while True:
            if self._interrupted_event.is_set():
                raise InterruptedError()

            try:
                return super().get(
                    block=True, timeout=self.INTERRUPTION_CHECK_FREQUENCY_SEC
                )
            except Empty:
                pass

    def put(self, item, block=True, timeout=None):
        if not block or timeout is not None:
            super().put(item, block, timeout)
            return

        # In case when the call is blocking and no timeout is specified (ie blocking
        # indefinitely) we apply the following protocol to make it interruptible:
        #
        #   1. `Queue.pet` is invoked w/ 500ms timeout
        #   2. `Full` exception is intercepted (will be raised upon timeout elapsing)
        #   3. If interrupted flag is set `InterruptedError` is raised
        #   4. Otherwise, protocol retried (until interrupted or queue
        #      becomes non-full)
        while True:
            if self._interrupted_event.is_set():
                raise InterruptedError()

            try:
                super().put(
                    item, block=True, timeout=self.INTERRUPTION_CHECK_FREQUENCY_SEC
                )
                return
            except Full:
                pass


def make_async_gen(
    base_iterator: Iterator[T],
    fn: Callable[[Iterator[T]], Iterator[U]],
    preserve_ordering: bool,
    num_workers: int = 1,
    buffer_size: int = 1,
) -> Generator[U, None, None]:
    """Returns a generator (iterator) mapping items from the
    provided iterator applying provided transformation in parallel (using a
    thread-pool).

    NOTE: There are some important constraints that needs to be carefully
          understood before using this method

        1. If `preserve_ordering` is True
            a. This method would unroll input iterator eagerly (irrespective
                of the speed of resulting generator being consumed). This is necessary
                as we can not guarantee liveness of the algorithm AND preserving of the
                original ordering at the same time.

            b. Resulting ordering of the output will "match" ordering of the input, ie
               that:
                    iterator = [A1, A2, ... An]
                    output iterator = [map(A1), map(A2), ..., map(An)]

        2. If `preserve_ordering` is False
            a. No more than `num_workers * (queue_buffer_size + 1)` elements will be
                fetched from the iterator

            b. Resulting ordering of the output is unspecified (and is
            non-deterministic)

    Args:
        base_iterator: Iterator yielding elements to map
        fn: Transformation to apply to each element
        preserve_ordering: Whether ordering has to be preserved
        num_workers: The number of threads to use in the threadpool (defaults to 1)
        buffer_size: Number of objects to be buffered in its input/output
                     queues (per queue; defaults to 2). Total number of objects held
                     in memory could be calculated as:

                        num_workers * buffer_size * 2 (input and output)

    Returns:
        An generator (iterator) of the elements corresponding to the source
        elements mapped by provided transformation (while *preserving the ordering*)
    """

    gen_id = random.randint(0, 2**31 - 1)

    if num_workers < 1:
        raise ValueError("Size of threadpool must be at least 1.")

    # Signal handler used to interrupt workers when terminating
    interrupted_event = threading.Event()

    # To apply transformations to elements in parallel *and* preserve the ordering
    # following invariants are established:
    #   - Every worker is handled by standalone thread
    #   - Every worker is assigned an input and an output queue
    #
    # And following protocol is implemented:
    #   - Filling worker traverses input iterator round-robin'ing elements across
    #     the input queues (in order!)
    #   - Transforming workers traverse respective input queue in-order: de-queueing
    #     element, applying transformation and enqueuing the result into the output
    #     queue
    #   - Generator (returned from this method) traverses output queues (in the same
    #     order as input queues) dequeues 1 mapped element at a time from each output
    #     queue and yields it
    #
    # However, in case when we're preserving the ordering we can not enforce the input
    # queue size as this could result in deadlocks since transformations could be
    # producing sequences of arbitrary length.
    #
    # Check `test_make_async_gen_varying_seq_length_stress_test` for more context on
    # this problem.
    if preserve_ordering:
        input_queue_buf_size = -1
        num_input_queues = num_workers
    else:
        input_queue_buf_size = (buffer_size + 1) * num_workers
        num_input_queues = 1

    input_queues = [
        _InterruptibleQueue(input_queue_buf_size, interrupted_event)
        for _ in range(num_input_queues)
    ]

    output_queues = [
        _InterruptibleQueue(buffer_size, interrupted_event) for _ in range(num_workers)
    ]

    # Filling worker
    def _run_filling_worker():
        try:
            # First, round-robin elements from the iterator into
            # corresponding input queues (one by one)
            for idx, item in enumerate(base_iterator):
                input_queues[idx % num_input_queues].put(item)

            # NOTE: We have to Enqueue sentinel objects for every transforming
            #       worker:
            #   - In case of preserving order of ``num_queues`` == ``num_workers``
            #     we will enqueue 1 sentinel per queue
            #   - In case of NOT preserving order all ``num_workers`` sentinels
            #     will be enqueued into a single queue
            for idx in range(num_workers):
                input_queues[idx % num_input_queues].put(SENTINEL)

        except InterruptedError:
            pass

        except Exception as e:
            logger.warning("Caught exception in filling worker!", exc_info=e)
            # In case of filling worker encountering an exception we have to propagate
            # it back to the (main) iterating thread. To achieve that we're traversing
            # output queues *backwards* relative to the order of iterator-thread such
            # that they are more likely to meet w/in a single iteration.
            for output_queue in reversed(output_queues):
                output_queue.put(e)

    # Transforming worker
    def _run_transforming_worker(input_queue, output_queue):
        try:
            # Create iterator draining the queue, until it receives sentinel
            #
            # NOTE: `queue.get` is blocking!
            input_queue_iter = iter(input_queue.get, SENTINEL)

            for result in fn(input_queue_iter):
                # Enqueue result of the transformation
                output_queue.put(result)

            # Enqueue sentinel (to signal that transformations are completed)
            output_queue.put(SENTINEL)

        except InterruptedError:
            pass

        except Exception as e:
            logger.warning("Caught exception in transforming worker!", exc_info=e)
            # NOTE: In this case we simply enqueue the exception rather than
            #       interrupting
            output_queue.put(e)

    # Start workers threads
    filling_worker_thread = threading.Thread(
        target=_run_filling_worker,
        name=f"map_tp_filling_worker-{gen_id}",
        daemon=True,
    )
    filling_worker_thread.start()

    transforming_worker_threads = [
        threading.Thread(
            target=_run_transforming_worker,
            name=f"map_tp_transforming_worker-{gen_id}-{idx}",
            args=(input_queues[idx % num_input_queues], output_queues[idx]),
            daemon=True,
        )
        for idx in range(num_workers)
    ]

    for t in transforming_worker_threads:
        t.start()

    # Use main thread to yield output batches
    try:
        # Keep track of remaining non-empty output queues
        remaining_output_queues = output_queues

        while len(remaining_output_queues) > 0:
            # To provide deterministic ordering of the produced iterator we rely
            # on the following invariants:
            #
            #   - Elements from the original iterator are round-robin'd into
            #     input queues (in order)
            #   - Individual workers drain their respective input queues populating
            #     output queues with the results of applying transformation to the
            #     original item (and hence preserving original ordering of the input
            #     queue)
            #   - To yield from the generator output queues are traversed in the same
            #     order and one single element is dequeued (in a blocking way!) at a
            #     time from every individual output queue
            #
            empty_queues = []

            # At every iteration only remaining non-empty queues
            # are traversed (to prevent blocking on exhausted queue)
            for output_queue in remaining_output_queues:
                # NOTE: This is blocking!
                item = output_queue.get()

                if isinstance(item, Exception):
                    raise item

                if item is SENTINEL:
                    empty_queues.append(output_queue)
                else:
                    yield item

            if empty_queues:
                remaining_output_queues = [
                    q for q in remaining_output_queues if q not in empty_queues
                ]

    finally:
        # Set flag to interrupt workers (to make sure no dangling
        # threads holding the objects are left behind)
        #
        # NOTE: Interrupted event is set to interrupt the running threads
        #       that might be blocked otherwise waiting on inputs from respective
        #       queues. However, even though we're interrupting the threads we can't
        #       guarantee that threads will be interrupted in time (as this is
        #       dependent on Python's GC finalizer to close the generator by raising
        #       `GeneratorExit`) and hence we can't join on either filling or
        #       transforming workers.
        interrupted_event.set()


class RetryingContextManager:
    def __init__(
        self,
        f: pyarrow.NativeFile,
        context: DataContext,
        max_attempts: int = 10,
        max_backoff_s: int = 32,
    ):
        self._f = f
        self._data_context = context
        self._max_attempts = max_attempts
        self._max_backoff_s = max_backoff_s

    def __repr__(self):
        return f"<{self.__class__.__name__} fs={self.handler.unwrap()}>"

    def _retry_operation(self, operation: Callable, description: str):
        """Execute an operation with retries."""
        return call_with_retry(
            operation,
            description=description,
            match=self._data_context.retried_io_errors,
            max_attempts=self._max_attempts,
            max_backoff_s=self._max_backoff_s,
        )

    def __enter__(self):
        return self._retry_operation(self._f.__enter__, "enter file context")

    def __exit__(self, exc_type, exc_value, traceback):
        self._retry_operation(
            lambda: self._f.__exit__(exc_type, exc_value, traceback),
            "exit file context",
        )


class RetryingPyFileSystem(pyarrow.fs.PyFileSystem):
    def __init__(self, handler: "RetryingPyFileSystemHandler"):
        if not isinstance(handler, RetryingPyFileSystemHandler):
            assert ValueError("handler must be a RetryingPyFileSystemHandler")
        super().__init__(handler)

    @property
    def retryable_errors(self) -> List[str]:
        return self.handler._retryable_errors

    def unwrap(self):
        return self.handler.unwrap()

    @classmethod
    def wrap(
        cls,
        fs: "pyarrow.fs.FileSystem",
        retryable_errors: List[str],
        max_attempts: int = 10,
        max_backoff_s: int = 32,
    ):
        if isinstance(fs, RetryingPyFileSystem):
            return fs
        handler = RetryingPyFileSystemHandler(
            fs, retryable_errors, max_attempts, max_backoff_s
        )
        return cls(handler)

    def __reduce__(self):
        # Serialization of this class breaks for some reason without this
        return (self.__class__, (self.handler,))

    @classmethod
    def __setstate__(cls, state):
        # Serialization of this class breaks for some reason without this
        return cls(*state)


class RetryingPyFileSystemHandler(pyarrow.fs.FileSystemHandler):
    """Wrapper for filesystem objects that adds retry functionality for file operations.

    This class wraps any filesystem object and adds automatic retries for common
    file operations that may fail transiently.
    """

    def __init__(
        self,
        fs: "pyarrow.fs.FileSystem",
        retryable_errors: List[str] = tuple(),
        max_attempts: int = 10,
        max_backoff_s: int = 32,
    ):
        """Initialize the retrying filesystem wrapper.

        Args:
            fs: The underlying filesystem to wrap
            context: DataContext for retry settings
            max_attempts: Maximum number of retry attempts
            max_backoff_s: Maximum backoff time in seconds
        """
        assert not isinstance(
            fs, RetryingPyFileSystem
        ), "Cannot wrap a RetryingPyFileSystem"
        self._fs = fs
        self._retryable_errors = retryable_errors
        self._max_attempts = max_attempts
        self._max_backoff_s = max_backoff_s

    def _retry_operation(self, operation: Callable, description: str):
        """Execute an operation with retries."""
        return call_with_retry(
            operation,
            description=description,
            match=self._retryable_errors,
            max_attempts=self._max_attempts,
            max_backoff_s=self._max_backoff_s,
        )

    def unwrap(self):
        return self._fs

    def copy_file(self, src: str, dest: str):
        """Copy a file."""
        return self._retry_operation(
            lambda: self._fs.copy_file(src, dest), f"copy file from {src} to {dest}"
        )

    def create_dir(self, path: str, recursive: bool):
        """Create a directory and subdirectories."""
        return self._retry_operation(
            lambda: self._fs.create_dir(path, recursive=recursive),
            f"create directory {path}",
        )

    def delete_dir(self, path: str):
        """Delete a directory and its contents, recursively."""
        return self._retry_operation(
            lambda: self._fs.delete_dir(path), f"delete directory {path}"
        )

    def delete_dir_contents(self, path: str, missing_dir_ok: bool = False):
        """Delete a directory's contents, recursively."""
        return self._retry_operation(
            lambda: self._fs.delete_dir_contents(path, missing_dir_ok=missing_dir_ok),
            f"delete directory contents {path}",
        )

    def delete_file(self, path: str):
        """Delete a file."""
        return self._retry_operation(
            lambda: self._fs.delete_file(path), f"delete file {path}"
        )

    def delete_root_dir_contents(self):
        return self._retry_operation(
            lambda: self._fs.delete_dir_contents("/", accept_root_dir=True),
            "delete root dir contents",
        )

    def equals(self, other: "pyarrow.fs.FileSystem") -> bool:
        """Test if this filesystem equals another."""
        return self._fs.equals(other)

    def get_file_info(self, paths: List[str]):
        """Get info for the given files."""
        return self._retry_operation(
            lambda: self._fs.get_file_info(paths),
            f"get file info for {paths}",
        )

    def get_file_info_selector(self, selector):
        return self._retry_operation(
            lambda: self._fs.get_file_info(selector),
            f"get file info for {selector}",
        )

    def get_type_name(self):
        return "RetryingPyFileSystem"

    def move(self, src: str, dest: str):
        """Move / rename a file or directory."""
        return self._retry_operation(
            lambda: self._fs.move(src, dest), f"move from {src} to {dest}"
        )

    def normalize_path(self, path: str) -> str:
        """Normalize filesystem path."""
        return self._retry_operation(
            lambda: self._fs.normalize_path(path), f"normalize path {path}"
        )

    def open_append_stream(
        self,
        path: str,
        metadata=None,
    ) -> "pyarrow.NativeFile":
        """Open an output stream for appending.

        Compression is disabled in this method because it is handled in the
        PyFileSystem abstract class.
        """
        return self._retry_operation(
            lambda: self._fs.open_append_stream(
                path,
                compression=None,
                metadata=metadata,
            ),
            f"open append stream for {path}",
        )

    def open_input_stream(
        self,
        path: str,
    ) -> "pyarrow.NativeFile":
        """Open an input stream for sequential reading.

        Compression is disabled in this method because it is handled in the
        PyFileSystem abstract class.
        """
        return self._retry_operation(
            lambda: self._fs.open_input_stream(path, compression=None),
            f"open input stream for {path}",
        )

    def open_output_stream(
        self,
        path: str,
        metadata=None,
    ) -> "pyarrow.NativeFile":
        """Open an output stream for sequential writing."

        Compression is disabled in this method because it is handled in the
        PyFileSystem abstract class.
        """
        return self._retry_operation(
            lambda: self._fs.open_output_stream(
                path,
                compression=None,
                metadata=metadata,
            ),
            f"open output stream for {path}",
        )

    def open_input_file(self, path: str) -> "pyarrow.NativeFile":
        """Open an input file for random access reading."""
        return self._retry_operation(
            lambda: self._fs.open_input_file(path), f"open input file {path}"
        )


def call_with_retry(
    f: Callable[[], Any],
    description: str,
    *,
    match: Optional[List[str]] = None,
    max_attempts: int = 10,
    max_backoff_s: int = 32,
) -> Any:
    """Retry a function with exponential backoff.

    Args:
        f: The function to retry.
        match: A list of strings to match in the exception message. If ``None``, any
            error is retried.
        description: An imperitive description of the function being retried. For
            example, "open the file".
        max_attempts: The maximum number of attempts to retry.
        max_backoff_s: The maximum number of seconds to backoff.
    """
    assert max_attempts >= 1, f"`max_attempts` must be positive. Got {max_attempts}."

    for i in range(max_attempts):
        try:
            return f()
        except Exception as e:
            is_retryable = match is None or any(pattern in str(e) for pattern in match)
            if is_retryable and i + 1 < max_attempts:
                # Retry with binary expoential backoff with random jitter.
                backoff = min((2 ** (i + 1)), max_backoff_s) * (random.random())
                logger.debug(
                    f"Retrying {i+1} attempts to {description} after {backoff} seconds."
                )
                time.sleep(backoff)
            else:
                logger.debug(
                    f"Did not find a match for {str(e)}. Raising after {i+1} attempts."
                )
                raise e from None


def iterate_with_retry(
    iterable_factory: Callable[[], Iterable],
    description: str,
    *,
    match: Optional[List[str]] = None,
    max_attempts: int = 10,
    max_backoff_s: int = 32,
) -> Any:
    """Iterate through an iterable with retries.

    If the iterable raises an exception, this function recreates and re-iterates
    through the iterable, while skipping the items that have already been yielded.

    Args:
        iterable_factory: A no-argument function that creates the iterable.
        match: A list of strings to match in the exception message. If ``None``, any
            error is retried.
        description: An imperitive description of the function being retried. For
            example, "open the file".
        max_attempts: The maximum number of attempts to retry.
        max_backoff_s: The maximum number of seconds to backoff.
    """
    assert max_attempts >= 1, f"`max_attempts` must be positive. Got {max_attempts}."

    num_items_yielded = 0
    for attempt in range(max_attempts):
        try:
            iterable = iterable_factory()
            for item_index, item in enumerate(iterable):
                if item_index < num_items_yielded:
                    # Skip items that have already been yielded.
                    continue

                num_items_yielded += 1
                yield item
            return
        except Exception as e:
            is_retryable = match is None or any(pattern in str(e) for pattern in match)
            if is_retryable and attempt + 1 < max_attempts:
                # Retry with binary expoential backoff with random jitter.
                backoff = min((2 ** (attempt + 1)), max_backoff_s) * random.random()
                logger.debug(
                    f"Retrying {attempt+1} attempts to {description} "
                    f"after {backoff} seconds."
                )
                time.sleep(backoff)
            else:
                raise e from None


def convert_bytes_to_human_readable_str(num_bytes: int) -> str:
    if num_bytes >= 1e9:
        num_bytes_str = f"{round(num_bytes / 1e9)}GB"
    elif num_bytes >= 1e6:
        num_bytes_str = f"{round(num_bytes / 1e6)}MB"
    else:
        num_bytes_str = f"{round(num_bytes / 1e3)}KB"
    return num_bytes_str


def _validate_rows_per_file_args(
    *,
    num_rows_per_file: Optional[int] = None,
    min_rows_per_file: Optional[int] = None,
    max_rows_per_file: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int]]:
    """Helper method to validate and handle rows per file arguments.

    Args:
        num_rows_per_file: Deprecated parameter for number of rows per file
        min_rows_per_file: New parameter for minimum rows per file
        max_rows_per_file: New parameter for maximum rows per file

    Returns:
        A tuple of (effective_min_rows_per_file, effective_max_rows_per_file)
    """
    if num_rows_per_file is not None:
        import warnings

        warnings.warn(
            "`num_rows_per_file` is deprecated and will be removed in a future release. "
            "Use `min_rows_per_file` instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        if min_rows_per_file is not None:
            raise ValueError(
                "Cannot specify both `num_rows_per_file` and `min_rows_per_file`. "
                "Use `min_rows_per_file` as `num_rows_per_file` is deprecated."
            )
        min_rows_per_file = num_rows_per_file

    # Validate max_rows_per_file
    if max_rows_per_file is not None and max_rows_per_file <= 0:
        raise ValueError("max_rows_per_file must be a positive integer")

    # Validate min_rows_per_file
    if min_rows_per_file is not None and min_rows_per_file <= 0:
        raise ValueError("min_rows_per_file must be a positive integer")

    # Validate that max >= min if both are specified
    if (
        min_rows_per_file is not None
        and max_rows_per_file is not None
        and min_rows_per_file > max_rows_per_file
    ):
        raise ValueError(
            f"min_rows_per_file ({min_rows_per_file}) cannot be greater than "
            f"max_rows_per_file ({max_rows_per_file})"
        )

    return min_rows_per_file, max_rows_per_file


def is_nan(value) -> bool:
    """Returns true if provide value is ``np.nan``"""

    try:
        return isinstance(value, float) and np.isnan(value)
    except TypeError:
        return False


def is_null(value: Any) -> bool:
    """This generalization of ``is_nan`` util qualifying both None and np.nan
    as null values"""
    return value is None or is_nan(value)


def keys_equal(keys1, keys2):
    if len(keys1) != len(keys2):
        return False
    for k1, k2 in zip(keys1, keys2):
        if not ((is_nan(k1) and is_nan(k2)) or k1 == k2):
            return False
    return True


def get_total_obj_store_mem_on_node() -> int:
    """Return the total object store memory on the current node.

    This function incurs an RPC. Use it cautiously.
    """
    node_id = ray.get_runtime_context().get_node_id()
    total_resources_per_node = ray._private.state.total_resources_per_node()
    assert (
        node_id in total_resources_per_node
    ), f"Expected node '{node_id}' to be in resources: {total_resources_per_node}"
    return total_resources_per_node[node_id]["object_store_memory"]


class MemoryProfiler:
    """A context manager that polls the USS of the current process.

    This class approximates the max USS by polling memory and subtracting the amount
    of shared memory from the resident set size (RSS). It's not a
    perfect estimate (it can underestimate, e.g., if you use Torch tensors), but
    estimating the USS is much cheaper than computing the actual USS.

    .. warning::

        This class only works with Linux. If you use it on another platform,
        `estimate_max_uss` always returns ``None``.

    Example:

        .. testcode::

            with MemoryProfiler(poll_interval_s=1.0) as profiler:
                for i in range(10):
                    ...  # Your code here
                    print(f"Max USS: {profiler.estimate_max_uss()}")
                    profiler.reset()
    """

    def __init__(self, poll_interval_s: Optional[float]):
        """

        Args:
            poll_interval_s: The interval to poll the USS of the process. If `None`,
                this class won't poll the USS.
        """
        self._poll_interval_s = poll_interval_s

        self._process = psutil.Process(os.getpid())
        self._max_uss = None
        self._max_uss_lock = threading.Lock()

        self._uss_poll_thread = None
        self._stop_uss_poll_event = None

    def __repr__(self):
        return f"MemoryProfiler(poll_interval_s={self._poll_interval_s})"

    def __enter__(self):
        if self._can_estimate_uss() and self._poll_interval_s is not None:
            (
                self._uss_poll_thread,
                self._stop_uss_poll_event,
            ) = self._start_uss_poll_thread()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._uss_poll_thread is not None:
            self._stop_uss_poll_thread()

    def estimate_max_uss(self) -> Optional[int]:
        """Get an estimate of the max USS of the current process.

        Returns:
            An estimate of the max USS of the process in bytes, or ``None`` if an
            estimate isn't available.
        """
        if not self._can_estimate_uss():
            assert self._max_uss is None
            return None

        with self._max_uss_lock:
            if self._max_uss is None:
                self._max_uss = self._estimate_uss()
            else:
                self._max_uss = max(self._max_uss, self._estimate_uss())

        assert self._max_uss is not None
        return self._max_uss

    def reset(self):
        with self._max_uss_lock:
            self._max_uss = None

    def _start_uss_poll_thread(self) -> Tuple[threading.Thread, threading.Event]:
        assert self._poll_interval_s is not None
        assert self._can_estimate_uss()

        stop_event = threading.Event()

        def poll_uss():
            while not stop_event.is_set():
                with self._max_uss_lock:
                    if self._max_uss is None:
                        self._max_uss = self._estimate_uss()
                    else:
                        self._max_uss = max(self._max_uss, self._estimate_uss())
                stop_event.wait(self._poll_interval_s)

        thread = threading.Thread(target=poll_uss, daemon=True)
        thread.start()

        return thread, stop_event

    def _stop_uss_poll_thread(self):
        if self._stop_uss_poll_event is not None:
            self._stop_uss_poll_event.set()
            self._uss_poll_thread.join()

    def _estimate_uss(self) -> int:
        assert self._can_estimate_uss()
        memory_info = self._process.memory_info()
        # Estimate the USS (the amount of memory that'd be free if we killed the
        # process right now) as the difference between the RSS (total physical memory)
        # and amount of shared physical memory.
        return memory_info.rss - memory_info.shared

    @staticmethod
    @functools.cache
    def _can_estimate_uss() -> bool:
        # MacOS and Windows don't have the 'shared' attribute of `memory_info()`.
        return platform.system() == "Linux"


def unzip(data: List[Tuple[Any, ...]]) -> Tuple[List[Any], ...]:
    """Unzips a list of tuples into a tuple of lists

    Args:
        data: A list of tuples to unzip.

    Returns:
        A tuple of lists, where each list corresponds to one element of the tuples in
        the input list.
    """
    return tuple(map(list, zip(*data)))


def rows_same(actual: pd.DataFrame, expected: pd.DataFrame) -> bool:
    """Check if two DataFrames have the same rows.

    Unlike the built-in pandas equals method, this function ignores indices and the
    order of rows. This is useful for testing Ray Data because its interface doesn't
    usually guarantee the order of rows.
    """
    actual_rows = actual.to_dict(orient="records")
    expected_rows = expected.to_dict(orient="records")

    actual_items_counts = Counter(frozenset(row.items()) for row in actual_rows)
    expected_items_counts = Counter(frozenset(row.items()) for row in expected_rows)

    return actual_items_counts == expected_items_counts
