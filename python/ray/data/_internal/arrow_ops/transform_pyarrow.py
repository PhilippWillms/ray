import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Union

import numpy as np
from packaging.version import parse as parse_version

from ray._private.arrow_utils import get_pyarrow_version
from ray._private.ray_constants import env_integer
from ray._private.utils import INT32_MAX
from ray.air.util.tensor_extensions.arrow import (
    MIN_PYARROW_VERSION_CHUNKED_ARRAY_TO_NUMPY_ZERO_COPY_ONLY,
    PYARROW_VERSION,
)

try:
    import pyarrow
except ImportError:
    pyarrow = None


# Minimum version support {String,List,Binary}View types
MIN_PYARROW_VERSION_VIEW_TYPES = parse_version("16.0.0")
MIN_PYARROW_VERSION_TYPE_PROMOTION = parse_version("14.0.0")


# pyarrow.Table.slice is slow when the table has many chunks
# so we combine chunks into a single one to make slice faster
# with the cost of an extra copy.
#
# The decision to combine chunks is based on a threshold for the number
# of chunks, set by `MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS`. To make
# this more flexible, we have made this threshold configurable via the
# `RAY_DATA_MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS` environment variable.
#
# This configurability is important because the size of each chunk can vary
# greatly depending on the dataset and the operations performed previously.
# A fixed threshold might not be optimal for all scenarios, as in some cases,
# a smaller number of large chunks could behave differently from a larger
# number of smaller chunks. By making this threshold tunable, users have
# the ability to optimize for their specific case, adjusting based on their
# chunk sizes and available memory.
# See https://github.com/ray-project/ray/issues/31108 for more details.
# TODO(jjyao): remove this once https://github.com/apache/arrow/issues/35126 is resolved
MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS = env_integer(
    "RAY_DATA_MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS", 10
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from ray.data._internal.planner.exchange.sort_task_spec import SortKey


def sort(table: "pyarrow.Table", sort_key: "SortKey") -> "pyarrow.Table":
    import pyarrow.compute as pac

    indices = pac.sort_indices(table, sort_keys=sort_key.to_arrow_sort_args())
    return take_table(table, indices)


def _create_empty_table(schema: "pyarrow.Schema"):
    import pyarrow as pa

    arrays = [pa.array([], type=t) for t in schema.types]

    return pa.table(arrays, schema=schema)


def hash_partition(
    table: "pyarrow.Table",
    *,
    hash_cols: List[str],
    num_partitions: int,
) -> Dict[int, "pyarrow.Table"]:
    """Hash-partitions provided Pyarrow `Table` into `num_partitions` based on
    hash of the composed tuple of values from the provided columns list

    NOTE: Since some partitions could be empty (due to skew in the table) this returns a
          dictionary, rather than a list
    """

    import numpy as np

    assert num_partitions > 0

    if table.num_rows == 0:
        return {}
    elif num_partitions == 1:
        return {0: table}

    projected_table = table.select(hash_cols)

    partitions = np.zeros((projected_table.num_rows,))
    for i in range(projected_table.num_rows):
        _tuple = tuple(c[i] for c in projected_table.columns)
        partitions[i] = hash(_tuple) % num_partitions

    # Convert to ndarray to compute hash partition indices
    # more efficiently
    partitions_array = np.asarray(partitions)
    # For every partition compile list of indices of rows falling
    # under that partition
    indices = [np.where(partitions_array == p)[0] for p in range(num_partitions)]

    # NOTE: Subsequent `take` operation is known to be sensitive to the number of
    #       chunks w/in the individual columns, and therefore to improve performance
    #       we attempt to defragment the table to potentially combine some of those
    #       chunks into contiguous arrays.
    table = try_combine_chunked_columns(table)

    return {
        p: table.take(idx)
        # NOTE: Since some of the partitions might be empty, we're filtering out
        #       indices of the length 0 to make sure we're not passing around
        #       empty tables
        for p, idx in enumerate(indices)
        if len(idx) > 0
    }


def take_table(
    table: "pyarrow.Table",
    indices: Union[List[int], np.ndarray, "pyarrow.Array", "pyarrow.ChunkedArray"],
) -> "pyarrow.Table":
    """Select rows from the table.

    This method is an alternative to pyarrow.Table.take(), which breaks for
    extension arrays. This is exposed as a static method for easier use on
    intermediate tables, not underlying an ArrowBlockAccessor.
    """
    from ray.air.util.transform_pyarrow import (
        _concatenate_extension_column,
        _is_column_extension_type,
    )

    if any(_is_column_extension_type(col) for col in table.columns):
        new_cols = []
        for col in table.columns:
            if _is_column_extension_type(col) and col.num_chunks > 1:
                # .take() will concatenate internally, which currently breaks for
                # extension arrays.
                col = _concatenate_extension_column(col)
            new_cols.append(col.take(indices))
        table = pyarrow.Table.from_arrays(new_cols, schema=table.schema)
    else:
        table = table.take(indices)
    return table


def unify_schemas(
    schemas: List["pyarrow.Schema"], *, promote_types: bool = False
) -> "pyarrow.Schema":
    """Version of `pyarrow.unify_schemas()` which also handles checks for
    variable-shaped tensors in the given schemas.

    This function scans all input schemas to identify columns that contain
    variable-shaped tensors or objects. For tensor columns, it ensures the
    use of appropriate tensor types (including variable-shaped tensor types).
    For object columns, it uses a specific object type to accommodate any
    objects present. Additionally, it handles columns with null-typed lists
    by determining their actual types from the given schemas.

    Currently, it disallows the concatenation of tensor columns and
    pickled object columsn for performance reasons.
    """
    import pyarrow as pa

    from ray.air.util.object_extensions.arrow import ArrowPythonObjectType
    from ray.air.util.tensor_extensions.arrow import (
        ArrowTensorType,
        ArrowVariableShapedTensorType,
    )

    schemas_to_unify = []
    schema_field_overrides = {}

    # Rollup columns with opaque (null-typed) lists, to override types in
    # the following for-loop.
    cols_with_null_list = set()

    all_columns = set()
    for schema in schemas:
        for col_name in schema.names:
            # Check for duplicate field names in this schema
            if schema.names.count(col_name) > 1:
                # This is broken for Pandas blocks and broken with the logic here
                raise ValueError(
                    f"Schema {schema} has multiple fields with the same name: {col_name}"
                )
            col_type = schema.field(col_name).type
            if pa.types.is_list(col_type) and pa.types.is_null(col_type.value_type):
                cols_with_null_list.add(col_name)
            all_columns.add(col_name)

    from ray.air.util.tensor_extensions.arrow import (
        get_arrow_extension_fixed_shape_tensor_types,
        get_arrow_extension_tensor_types,
    )

    arrow_tensor_types = get_arrow_extension_tensor_types()
    arrow_fixed_shape_tensor_types = get_arrow_extension_fixed_shape_tensor_types()

    columns_with_objects = set()
    columns_with_tensor_array = set()
    columns_with_struct = set()
    for col_name in all_columns:
        for s in schemas:
            if col_name in s.names:
                if isinstance(s.field(col_name).type, ArrowPythonObjectType):
                    columns_with_objects.add(col_name)
                if isinstance(s.field(col_name).type, arrow_tensor_types):
                    columns_with_tensor_array.add(col_name)
                if isinstance(s.field(col_name).type, pa.StructType):
                    columns_with_struct.add(col_name)

    if len(columns_with_objects.intersection(columns_with_tensor_array)) > 0:
        # This is supportable if we use object type, but it will be expensive
        raise ValueError(
            "Found columns with both objects and tensors: "
            f"{columns_with_tensor_array.intersection(columns_with_objects)}"
        )
    for col_name in columns_with_tensor_array:
        tensor_array_types = [
            s.field(col_name).type
            for s in schemas
            if col_name in s.names
            and isinstance(s.field(col_name).type, arrow_tensor_types)
        ]

        # Check if we have missing tensor fields (some schemas don't have this field)
        has_missing_fields = len(tensor_array_types) < len(schemas)

        # Convert to variable-shaped if needed or if we have missing fields
        if (
            ArrowTensorType._need_variable_shaped_tensor_array(tensor_array_types)
            or has_missing_fields
        ):
            if isinstance(tensor_array_types[0], ArrowVariableShapedTensorType):
                new_type = tensor_array_types[0]
            elif isinstance(tensor_array_types[0], arrow_fixed_shape_tensor_types):
                new_type = ArrowVariableShapedTensorType(
                    dtype=tensor_array_types[0].scalar_type,
                    ndim=len(tensor_array_types[0].shape),
                )
            else:
                raise ValueError(
                    "Detected need for variable shaped tensor representation, "
                    f"but schema is not ArrayTensorType: {tensor_array_types[0]}"
                )
            schema_field_overrides[col_name] = new_type

    for col_name in columns_with_objects:
        schema_field_overrides[col_name] = ArrowPythonObjectType()

    for col_name in columns_with_struct:
        field_types = [s.field(col_name).type for s in schemas]

        # Unify struct schemas
        struct_schemas = []
        for t in field_types:
            if t is not None and pa.types.is_struct(t):
                struct_schemas.append(pa.schema(list(t)))
            else:
                struct_schemas.append(pa.schema([]))

        unified_struct_schema = unify_schemas(
            struct_schemas, promote_types=promote_types
        )

        schema_field_overrides[col_name] = pa.struct(list(unified_struct_schema))

    if cols_with_null_list:
        # For each opaque list column, iterate through all schemas until we find
        # a valid value_type that can be used to override the column types in
        # the following for-loop.
        for col_name in cols_with_null_list:
            for schema in schemas:
                col_type = schema.field(col_name).type
                if not pa.types.is_list(col_type) or not pa.types.is_null(
                    col_type.value_type
                ):
                    schema_field_overrides[col_name] = col_type
                    break

    if schema_field_overrides:
        # Go through all schemas and update the types of columns from the above loop.
        for schema in schemas:
            for col_name, col_new_type in schema_field_overrides.items():
                if col_name in schema.names:
                    var_shaped_col = schema.field(col_name).with_type(col_new_type)
                    col_idx = schema.get_field_index(col_name)
                    schema = schema.set(col_idx, var_shaped_col)
            schemas_to_unify.append(schema)
    else:
        schemas_to_unify = schemas

    try:
        if get_pyarrow_version() < MIN_PYARROW_VERSION_TYPE_PROMOTION:
            return pyarrow.unify_schemas(schemas_to_unify)

        # NOTE: By default type promotion (from "smaller" to "larger" types) is disabled,
        #       allowing only promotion b/w nullable and non-nullable ones
        arrow_promote_types_mode = "permissive" if promote_types else "default"

        return pyarrow.unify_schemas(
            schemas_to_unify, promote_options=arrow_promote_types_mode
        )
    except Exception as e:
        schemas_str = "\n-----\n".join([str(s) for s in schemas_to_unify])

        logger.error(f"Failed to unify schemas: {schemas_str}", exc_info=e)

        raise


def _concatenate_chunked_arrays(arrs: "pyarrow.ChunkedArray") -> "pyarrow.ChunkedArray":
    """
    Concatenate provided chunked arrays into a single chunked array.
    """
    from ray.data.extensions import get_arrow_extension_tensor_types

    tensor_types = get_arrow_extension_tensor_types()

    # Infer the type as the first non-null type.
    type_ = None
    for arr in arrs:
        assert not isinstance(arr.type, tensor_types), (
            "'_concatenate_chunked_arrays' should only be used on non-tensor "
            f"extension types, but got a chunked array of type {type_}."
        )
        if type_ is None and not pyarrow.types.is_null(arr.type):
            type_ = arr.type
            break

    if type_ is None:
        # All arrays are null, so the inferred type is null.
        type_ = pyarrow.null()

    # Single flat list of chunks across all chunked arrays.
    chunks = []
    for arr in arrs:
        if pyarrow.types.is_null(arr.type) and not pyarrow.types.is_null(type_):
            # If the type is null, we need to cast the array to the inferred type.
            arr = arr.cast(type_)
        elif not pyarrow.types.is_null(arr.type) and type_ != arr.type:
            raise RuntimeError(f"Types mismatch: {type_} != {arr.type}")

        # Add chunks for this chunked array to flat chunk list.
        chunks.extend(arr.chunks)

    # Construct chunked array on flat list of chunks.
    return pyarrow.chunked_array(chunks, type=type_)


def _extract_unified_struct_types(
    schema: "pyarrow.Schema",
) -> Dict[str, "pyarrow.StructType"]:
    """
    Extract all struct fields from a schema and map their names to types.

    Args:
        schema: Arrow schema to extract struct types from.

    Returns:
        Dict[str, pa.StructType]: Mapping of struct field names to their types.
    """
    import pyarrow as pa

    return {
        field.name: field.type for field in schema if pa.types.is_struct(field.type)
    }


def _backfill_missing_fields(
    column: "pyarrow.ChunkedArray",
    unified_struct_type: "pyarrow.StructType",
    block_length: int,
) -> "pyarrow.StructArray":
    """
    Align a struct column's fields to match the unified schema's struct type.

    Args:
        column: The column data to align.
        unified_struct_type: The unified struct type to align to.
        block_length: The number of rows in the block.

    Returns:
        pa.StructArray: The aligned struct array.
    """
    import pyarrow as pa

    from ray.air.util.tensor_extensions.arrow import (
        ArrowTensorType,
        ArrowVariableShapedTensorType,
        get_arrow_extension_tensor_types,
    )

    # Flatten chunked arrays into a single array if necessary
    if isinstance(column, pa.ChunkedArray):
        column = pa.concat_arrays(column.chunks)

    # Extract the current struct field names and their corresponding data
    current_fields = {
        field.name: column.field(i) for i, field in enumerate(column.type)
    }

    # Assert that the current fields are a subset of the unified struct type's field names
    unified_field_names = {field.name for field in unified_struct_type}
    assert set(current_fields.keys()).issubset(
        unified_field_names
    ), f"Fields {set(current_fields.keys())} are not a subset of unified struct fields {unified_field_names}."

    # Early exit if no fields are missing in the schema
    if column.type == unified_struct_type:
        return column

    tensor_types = get_arrow_extension_tensor_types()

    aligned_fields = []

    # Iterate over the fields in the unified struct type schema
    for field in unified_struct_type:
        field_name = field.name
        field_type = field.type

        if field_name in current_fields:
            # If the field exists in the current column, align it
            current_array = current_fields[field_name]
            if pa.types.is_struct(field_type):
                # Recursively align nested struct fields
                current_array = _backfill_missing_fields(
                    column=current_array,
                    unified_struct_type=field_type,
                    block_length=block_length,
                )

            # Handle tensor extension type mismatches
            elif isinstance(field_type, tensor_types) and isinstance(
                current_array.type, tensor_types
            ):
                # Convert to variable-shaped if needed
                if ArrowTensorType._need_variable_shaped_tensor_array(
                    [current_array.type, field_type]
                ) and not isinstance(current_array.type, ArrowVariableShapedTensorType):
                    # Only convert if it's not already a variable-shaped tensor array
                    current_array = current_array.to_variable_shaped_tensor_array()

            # The schema should already be unified by unify_schemas, so types
            # should be compatible. If not, let the error propagate up.
            # No explicit casting needed - PyArrow will handle type compatibility
            # during struct creation or raise appropriate errors.
            aligned_fields.append(current_array)
        else:
            # If the field is missing, fill with nulls
            aligned_fields.append(pa.nulls(block_length, type=field_type))

    # Reconstruct the struct column with aligned fields
    return pa.StructArray.from_arrays(
        aligned_fields,
        fields=unified_struct_type,
    )


def _align_struct_fields(
    blocks: List["pyarrow.Table"], schema: "pyarrow.Schema"
) -> List["pyarrow.Table"]:
    """
    Align struct columns across blocks to match the provided schema.

    Args:
        blocks: List of Arrow tables to align.
        schema: Unified schema with desired struct column alignment.

    Returns:
        List[pa.Table]: List of aligned Arrow tables.
    """
    import pyarrow as pa

    # Check if all block schemas are already aligned
    if all(block.schema == schema for block in blocks):
        return blocks

    # Extract all struct column types from the provided schema
    unified_struct_types = _extract_unified_struct_types(schema)

    # If there are no struct columns in the schema, return blocks as is
    if not unified_struct_types:
        return blocks

    aligned_blocks = []

    # Iterate over each block (table) in the list
    for block in blocks:
        # Store aligned struct columns
        aligned_columns = {}

        # Get the number of rows in the block
        block_length = len(block)

        # Process each struct column defined in the unified schema
        for column_name, unified_struct_type in unified_struct_types.items():
            # If the column exists in the block, align its fields
            if column_name in block.schema.names:
                column = block[column_name]

                # Check if the column type matches a struct type
                if isinstance(column.type, pa.StructType):
                    aligned_columns[column_name] = _backfill_missing_fields(
                        column, unified_struct_type, block_length
                    )
                else:
                    # If the column is not a struct, simply keep the original column
                    aligned_columns[column_name] = column
            else:
                # If the column is missing, create a null-filled column with the same
                # length as the block
                aligned_columns[column_name] = pa.array(
                    [None] * block_length, type=unified_struct_type
                )

        # Create a new aligned block with the updated columns and the unified schema.
        new_columns = []
        for column_name in schema.names:
            if column_name in aligned_columns:
                # Use the aligned column if available
                new_columns.append(aligned_columns[column_name])
            else:
                # Use the original column if not aligned
                assert column_name in block.schema.names
                new_columns.append(block[column_name])
        aligned_blocks.append(pa.table(new_columns, schema=schema))

    # Return the list of aligned blocks
    return aligned_blocks


def shuffle(block: "pyarrow.Table", seed: Optional[int] = None) -> "pyarrow.Table":
    """Shuffles provided Arrow table"""

    if len(block) == 0:
        return block

    indices = np.arange(block.num_rows)
    # Shuffle indices
    np.random.RandomState(seed).shuffle(indices)

    return take_table(block, indices)


def concat(
    blocks: List["pyarrow.Table"], *, promote_types: bool = False
) -> "pyarrow.Table":
    """Concatenate provided Arrow Tables into a single Arrow Table. This has special
    handling for extension types that pyarrow.concat_tables does not yet support.
    """
    import pyarrow as pa

    from ray.air.util.tensor_extensions.arrow import ArrowConversionError
    from ray.data.extensions import (
        ArrowPythonObjectArray,
        ArrowPythonObjectType,
        ArrowTensorArray,
        get_arrow_extension_tensor_types,
    )

    tensor_types = get_arrow_extension_tensor_types()

    if not blocks:
        # Short-circuit on empty list of blocks.
        return pa.table([])

    if len(blocks) == 1:
        return blocks[0]

    # If the result contains pyarrow schemas, unify them
    schemas_to_unify = [b.schema for b in blocks]
    try:
        schema = unify_schemas(schemas_to_unify, promote_types=promote_types)
    except Exception as e:
        raise ArrowConversionError(str(blocks)) from e

    # Handle alignment of struct type columns.
    blocks = _align_struct_fields(blocks, schema)

    # Rollup columns with opaque (null-typed) lists, to process in following for-loop.
    cols_with_null_list = set()
    for b in blocks:
        for col_name in b.schema.names:
            col_type = b.schema.field(col_name).type
            if pa.types.is_list(col_type) and pa.types.is_null(col_type.value_type):
                cols_with_null_list.add(col_name)

    if (
        any(isinstance(type_, pa.ExtensionType) for type_ in schema.types)
        or cols_with_null_list
    ):
        # Custom handling for extension array columns.
        cols = []
        for col_name in schema.names:
            col_chunked_arrays = []
            for block in blocks:
                col_chunked_arrays.append(block.column(col_name))

            if isinstance(schema.field(col_name).type, tensor_types):
                # For our tensor extension types, manually construct a chunked array
                # containing chunks from all blocks. This is to handle
                # homogeneous-shaped block columns having different shapes across
                # blocks: if tensor element shapes differ across blocks, a
                # variable-shaped tensor array will be returned.
                col = ArrowTensorArray._chunk_tensor_arrays(
                    [chunk for ca in col_chunked_arrays for chunk in ca.chunks]
                )
            elif isinstance(schema.field(col_name).type, ArrowPythonObjectType):
                chunks_to_concat = []
                # Cast everything to objects if concatenated with an object column
                for ca in col_chunked_arrays:
                    for chunk in ca.chunks:
                        if isinstance(ca.type, ArrowPythonObjectType):
                            chunks_to_concat.append(chunk)
                        else:
                            chunks_to_concat.append(
                                ArrowPythonObjectArray.from_objects(chunk.to_pylist())
                            )
                col = pa.chunked_array(chunks_to_concat)
            else:
                if col_name in cols_with_null_list:
                    # For each opaque list column, iterate through all schemas until
                    # we find a valid value_type that can be used to override the
                    # column types in the following for-loop.
                    scalar_type = None
                    for arr in col_chunked_arrays:
                        if not pa.types.is_list(arr.type) or not pa.types.is_null(
                            arr.type.value_type
                        ):
                            scalar_type = arr.type
                            break

                    if scalar_type is not None:
                        for c_idx in range(len(col_chunked_arrays)):
                            c = col_chunked_arrays[c_idx]
                            if pa.types.is_list(c.type) and pa.types.is_null(
                                c.type.value_type
                            ):
                                if pa.types.is_list(scalar_type):
                                    # If we are dealing with a list input,
                                    # cast the array to the scalar_type found above.
                                    col_chunked_arrays[c_idx] = c.cast(scalar_type)
                                else:
                                    # If we are dealing with a single value, construct
                                    # a new array with null values filled.
                                    col_chunked_arrays[c_idx] = pa.chunked_array(
                                        [pa.nulls(c.length(), type=scalar_type)]
                                    )

                col = _concatenate_chunked_arrays(col_chunked_arrays)
            cols.append(col)

        # Build the concatenated table.
        table = pyarrow.Table.from_arrays(cols, schema=schema)
        # Validate table schema (this is a cheap check by default).
        table.validate()
    else:
        # No extension array columns, so use built-in pyarrow.concat_tables.

        # When concatenating tables we allow type promotions to occur, since
        # no schema enforcement is currently performed, therefore allowing schemas
        # to vary b/w blocks
        #
        # NOTE: Type promotions aren't available in Arrow < 14.0
        if get_pyarrow_version() < parse_version("14.0.0"):
            table = pyarrow.concat_tables(blocks, promote=True)
        else:
            arrow_promote_types_mode = "permissive" if promote_types else "default"
            table = pyarrow.concat_tables(
                blocks, promote_options=arrow_promote_types_mode
            )

    return table


def concat_and_sort(
    blocks: List["pyarrow.Table"],
    sort_key: "SortKey",
    *,
    promote_types: bool = False,
) -> "pyarrow.Table":
    import pyarrow as pa
    import pyarrow.compute as pac

    if len(blocks) == 0:
        return pa.table([])

    ret = concat(blocks, promote_types=promote_types)
    indices = pac.sort_indices(ret, sort_keys=sort_key.to_arrow_sort_args())

    return take_table(ret, indices)


def table_to_numpy_dict_chunked(
    table: "pyarrow.Table",
) -> Dict[str, List[np.ndarray]]:
    """Convert a PyArrow table to a dictionary of lists of numpy arrays.

    Args:
        table: The PyArrow table to convert.

    Returns:
        A dictionary mapping column names to lists of numpy arrays. For chunked columns,
        the list will contain multiple arrays (one per chunk). For non-chunked columns,
        the list will contain a single array.
    """

    numpy_batch = {}
    for col_name in table.column_names:
        col = table[col_name]
        if isinstance(col, pyarrow.ChunkedArray):
            numpy_batch[col_name] = [
                to_numpy(chunk, zero_copy_only=False) for chunk in col.chunks
            ]
        else:
            numpy_batch[col_name] = [to_numpy(col, zero_copy_only=False)]
    return numpy_batch


def to_numpy(
    array: Union["pyarrow.Array", "pyarrow.ChunkedArray"],
    *,
    zero_copy_only: bool = True,
) -> np.ndarray:
    """Wrapper for `Array`s and `ChunkedArray`s `to_numpy` API,
    handling API divergence b/w Arrow versions"""

    import pyarrow as pa

    if isinstance(array, pa.Array):
        if pa.types.is_null(array.type):
            return np.full(len(array), np.nan, dtype=np.float32)
        return array.to_numpy(zero_copy_only=zero_copy_only)
    elif isinstance(array, pa.ChunkedArray):
        if pa.types.is_null(array.type):
            return np.full(array.length(), np.nan, dtype=np.float32)
        if PYARROW_VERSION >= MIN_PYARROW_VERSION_CHUNKED_ARRAY_TO_NUMPY_ZERO_COPY_ONLY:
            return array.to_numpy(zero_copy_only=zero_copy_only)
        else:
            return array.to_numpy()
    else:
        raise ValueError(
            f"Either of `Array` or `ChunkedArray` was expected, got {type(array)}"
        )


def try_combine_chunked_columns(table: "pyarrow.Table") -> "pyarrow.Table":
    """This method attempts to coalesce table by combining any of its
    columns exceeding threshold of `MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS`
    chunks in its `ChunkedArray`.

    This is necessary to improve performance for some operations (like `take`, etc)
    when dealing with `ChunkedArrays` w/ large number of chunks

    For more details check out https://github.com/apache/arrow/issues/35126
    """

    if table.num_columns == 0:
        return table

    new_column_values_arrays = []

    for col in table.columns:
        if col.num_chunks >= MIN_NUM_CHUNKS_TO_TRIGGER_COMBINE_CHUNKS:
            new_col = combine_chunked_array(col)
        else:
            new_col = col

        new_column_values_arrays.append(new_col)

    return pyarrow.Table.from_arrays(new_column_values_arrays, schema=table.schema)


def combine_chunks(table: "pyarrow.Table", copy: bool = False) -> "pyarrow.Table":
    """This is counterpart for Pyarrow's `Table.combine_chunks` that's using
    extended `ChunkedArray` combination protocol.

    For more details check out `combine_chunked_array` py-doc

    Args:
        table: Table with chunked columns to be combined into contiguous arrays.
        copy: Skip copying when copy is False and there is exactly 1 chunk.
    """

    new_column_values_arrays = []

    for col in table.columns:
        new_column_values_arrays.append(combine_chunked_array(col, copy))

    return pyarrow.Table.from_arrays(new_column_values_arrays, schema=table.schema)


def combine_chunked_array(
    array: "pyarrow.ChunkedArray",
    ensure_copy: bool = False,
) -> Union["pyarrow.Array", "pyarrow.ChunkedArray"]:
    """This is counterpart for Pyarrow's `ChunkedArray.combine_chunks` that additionally

        1. Handles `ExtensionType`s (like ArrowTensorType, ArrowTensorTypeV2,
           ArrowPythonObjectType, etc)

        2. Making sure `ChunkedArray`s comprising provided `Table` are combined
           safely, ie avoiding overflows of Arrow's internal offsets (using int32 for
           most of its native types, other than "large" kind).

    For more details check py-doc of `_try_combine_chunks_safe` method.

    Args:
        array: The chunked array to be combined into a single contiguous array.
        ensure_copy: Skip copying when ensure_copy is False and there's exactly
           1 chunk.
    """

    import pyarrow as pa

    from ray.air.util.transform_pyarrow import (
        _concatenate_extension_column,
        _is_column_extension_type,
    )

    assert isinstance(
        array, pa.ChunkedArray
    ), f"Expected `ChunkedArray`, got {type(array)}"

    if _is_column_extension_type(array):
        # Arrow `ExtensionArray`s can't be concatenated via `combine_chunks`,
        # hence require manual concatenation
        return _concatenate_extension_column(array, ensure_copy)
    elif len(array.chunks) == 0:
        # NOTE: In case there's no chunks, we need to explicitly create
        #       an empty array since calling into `combine_chunks` would fail
        #       due to it expecting at least 1 chunk to be present
        return pa.array([], type=array.type)
    elif len(array.chunks) == 1 and not ensure_copy:
        # Skip copying
        return array
    else:
        return _try_combine_chunks_safe(array)


# List of variable-width types using int64 offsets
_VARIABLE_WIDTH_INT64_OFFSET_PA_TYPE_PREDICATES = [
    pyarrow.types.is_large_list,
    pyarrow.types.is_large_string,
    pyarrow.types.is_large_binary,
]


# List of variable-width types using int32 offsets
_VARIABLE_WIDTH_INT32_OFFSET_PA_TYPE_PREDICATES = [
    pyarrow.types.is_string,
    pyarrow.types.is_binary,
    pyarrow.types.is_list,
    # Modeled as list<struct<key, val>>
    pyarrow.types.is_map,
]

if PYARROW_VERSION > MIN_PYARROW_VERSION_VIEW_TYPES:
    _VARIABLE_WIDTH_INT32_OFFSET_PA_TYPE_PREDICATES.extend(
        [
            pyarrow.types.is_string_view,
            pyarrow.types.is_binary_view,
            pyarrow.types.is_list_view,
        ]
    )


def _try_combine_chunks_safe(
    array: "pyarrow.ChunkedArray",
) -> Union["pyarrow.Array", "pyarrow.ChunkedArray"]:
    """This method provides a safe way of combining `ChunkedArray`s exceeding 2 GiB
    in size, which aren't using "large_*" types (and therefore relying on int32
    offsets).

    When handling provided `ChunkedArray` this method will be either

        - Relying on PyArrow's default `combine_chunks` (therefore returning single
        contiguous `Array`) in cases when
            - Array's total size is < 2 GiB
            - Array's underlying type is of "large" kind (ie using one of the
            `large_*` type family)
        - Safely combining subsets of tasks such that resulting `Array`s to not
        exceed 2 GiB in size (therefore returning another `ChunkedArray` albeit
        with potentially smaller number of chunks that have resulted from clumping
        the original ones)

    Args:
        array: The PyArrow ChunkedArray to safely combine.

    Returns:
        - ``pyarrow.Array`` if it's possible to combine provided ``pyarrow.ChunkedArray``
        into single contiguous array
        - ``pyarrow.ChunkedArray`` (albeit with chunks re-combined) if it's not possible to
        produce single pa.Array
    """

    import pyarrow as pa

    from ray.air.util.transform_pyarrow import _is_column_extension_type

    assert not _is_column_extension_type(
        array
    ), f"Arrow `ExtensionType`s are not accepted (got {array.type})"

    # It's safe to combine provided `ChunkedArray` in either of 2 cases:
    #   - It's type is NOT a variable-width type (list, binary, string, map),
    #     using int32 offsets into underlying data (bytes) array
    #   - It's type is a variable-width type using int64 offsets (large_list,
    #     large_string, etc)
    #   - It's cumulative byte-size is < INT32_MAX
    if (
        not any(p(array.type) for p in _VARIABLE_WIDTH_INT32_OFFSET_PA_TYPE_PREDICATES)
        or any(p(array.type) for p in _VARIABLE_WIDTH_INT64_OFFSET_PA_TYPE_PREDICATES)
        or array.nbytes < INT32_MAX
    ):
        return array.combine_chunks()

    # In this case it's actually *NOT* safe to try to directly combine
    # Arrow's `ChunkedArray` and is impossible to produce single, contiguous
    # `Array` since
    #     - It's of variable-width type that uses int32 offsets
    #     - It's cumulative estimated byte-size is > INT32_MAX (2 GiB)
    #
    # In this case instead of combining into single contiguous array, we
    # instead "clump" existing chunks into ones such that each of these is < INT32_MAX.
    #
    # NOTE: This branch actually returns `ChunkedArray` and not an `Array`

    new_chunks = []

    cur_chunk_group = []
    cur_chunk_group_size = 0

    for chunk in array.chunks:
        chunk_size = chunk.nbytes

        assert chunk_size <= INT32_MAX

        if cur_chunk_group_size + chunk_size > INT32_MAX:
            # Combine an accumulated group, append to the new list of chunks
            if cur_chunk_group:
                new_chunks.append(pa.concat_arrays(cur_chunk_group))

            cur_chunk_group = []
            cur_chunk_group_size = 0

        cur_chunk_group.append(chunk)
        cur_chunk_group_size += chunk_size

    # Add remaining chunks as last slice
    if cur_chunk_group:
        new_chunks.append(pa.concat_arrays(cur_chunk_group))

    return pa.chunked_array(new_chunks)
